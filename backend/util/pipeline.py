"""The weekly ETL, end to end.

Moved verbatim out of backend/main.py, which is now argument parsing and
dispatch and nothing else. The order of operations is unchanged: panels, then
the economic calendar, then the IMF refresh, then the per-country risk loop,
then the global alert ranking.

Every phase except the country loop is individually guarded, so one upstream
outage cannot take the run with it; the country loop guards each country the
same way. That was true before the move and is true after it.

This module lives in util/ because the ETL is the one piece of work that reaches
into every other folder - data_fetching, news_fetching, llm and data_upsert - so
it belongs to none of them.
"""

import json
import os
import pathlib
import time
import requests

from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

# --- Internal Imports -------------------------------------------
from backend.util import constants
from backend.util import db
from backend.util import hashing
from backend.util import vintage
from backend.util import provenance
from backend.util import usage
from backend.util import paths
from backend.data_fetching import curated_loader
from backend.data_fetching import structural_facts
from backend.data_fetching import data_retrieval
from backend.llm import constants as ai_constants
from backend.llm import langchain_llm
from backend.llm import digest_engine
from backend.llm import payload as payload_builder
from backend.llm import payload_health
from backend.llm import quality_report
from backend.llm import relevance
from backend.llm import calendar_ranker
from backend.llm import alerts_ranker
from backend.data_upsert import data_push, store
from backend.news_fetching import core
from backend.data_fetching import fetch_metrics
from backend.data_fetching import country_data_fetch
from backend.data_fetching import fmp_calendar_fetch
from backend.data_fetching import imf_macro_fetch
from backend.news_fetching.url_resolver import resolve_google_news_url
from backend.news_fetching.simple_scraper import get_article_assets
from backend.news_fetching.source_filter import is_blocked_url
from backend.news_fetching.advanced_scraper import scrape_one as crawlbase_scrape_one

# --- Paths ------------------------------------------------------------------
BACKEND_DIR    = paths.BACKEND_DIR
PROCESSED_DATA = paths.PANEL_DIR


# --- Helpers ----------------------------------------------------------------
def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

def _crawlbase_token() -> str:
    # Prefer JS token, then standard token
    return os.getenv("CRAWLBASE_JS_TOKEN") or os.getenv("CRAWLBASE_TOKEN") or ""

def _has_country_partition(root: pathlib.Path, iso2: str) -> bool:
    """
    Return True if a partition dir like country_code=XX exists, has at least one
    .parquet file, AND holds every column the registry currently expects.

    The column check is the part that matters. Adding an indicator to
    `INDICATOR_REGISTRY` used to be silent: the partition existed, so it was
    never rebuilt, so the new column never appeared, so the indicator resolved
    to nothing forever while the registry said it was expected. A registry entry
    nothing fetches is exactly the kind of writer-with-no-consumer this codebase
    keeps producing.
    """
    part_dir = root / f"country_code={iso2}"
    if not part_dir.is_dir():
        return False
    try:
        files = list(part_dir.glob("*.parquet"))
        if not files:
            return False
        import duckdb

        glob = (part_dir / "*.parquet").as_posix()
        have = {
            r[0]
            for r in duckdb.sql(
                f"SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet('{glob}'))"
            ).fetchall()
        }
        want = set(constants.ALL_INDICATORS)
        missing = want - have
        if missing:
            print(f"[{iso2}] panel is missing {sorted(missing)} — rebuilding.")
            return False
        return True
    except Exception:
        return False

def _parse_date_for_sort(date_str: str | None):
    """Parse publication date for sorting. Returns datetime(1970-01-01) for invalid/missing dates."""
    if not date_str:
        return datetime(1970, 1, 1)
    try:
        # Try ISO (allow trailing Z)
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except Exception:
        pass
    try:
        # Try date-only
        return datetime.strptime(date_str[:10], "%Y-%m-%d")
    except Exception:
        return datetime(1970, 1, 1)

def _fetch_candidate_pool(country_name: str, iso2: str) -> Tuple[List[Dict], Dict]:
    """Fetch one country's candidate pool. No ranking, no budget, no filtering.

    Retrieval lives in ``news_fetching.core``: six theme queries that match the
    ledgers the score is built from, ten deep, deduplicated on the resolved
    publisher link. What gets scored is decided afterwards, by the relevance
    gate — keeping the two apart is the point, because a keyword heuristic
    deciding what the model was allowed to read is the failure being removed.

    Returns:
        ``(candidates, pool_report)``.
    """
    pool = core.fetch_candidates(country_name, iso2)
    print(core.format_pool_report(pool["report"]))
    return pool["items"], pool["report"]


def _body_hash(a: Dict) -> str:
    """The hash of the body as read, or '' when there was none.

    The article's key alongside its URL, and what the manifest records so a
    week can find the exact text it read.
    """
    body = (a.get("text") or "")[:digest_engine.BODY_CAP_CHARS]
    return hashing.content_hash(body) if body else ""


def _article_rows(articles: List[Dict]) -> List[Dict]:
    """Build `article` rows for the evidence store.

    Only facts about the article. The country, how it was read and which theme
    query found it belong to the run, and go in the manifest.
    """
    rows = []
    for a in articles:
        url = a.get("publisher_link") or a.get("link")
        if not url:
            continue
        body = a.get("text") or ""
        rows.append({
            "url": url,
            "content_sha256": _body_hash(a),
            "wrapper_url": a.get("link"),
            "source_system": "google-news",
            "publisher": a.get("source"),
            "published_at": a.get("published"),
            "page_published_at": a.get("page_published_at"),
            "title": a.get("title"),
            "abstract": a.get("snippet"),
            "body": (body[:digest_engine.BODY_CAP_CHARS] or None),
            "body_chars_original": a.get("body_chars_original", len(body)) or None,
            "body_clipped": bool(a.get("body_clipped", len(body) > digest_engine.BODY_CAP_CHARS)),
        })
    return rows


def ensure_missing_country_panels(root: pathlib.Path,
                                  indicators: dict,
                                  start: int | None = None,
                                  end: int | None = None,
                                  roster: Optional[List[Dict]] = None) -> None:
    """
    Make sure every country in constants.COUNTRY_ROSTER has a partition under root.
    Only (re)build and write partitions that are missing or empty.
    """
    root.mkdir(parents=True, exist_ok=True)

    roster = roster or constants.COUNTRY_ROSTER
    iso3_by_iso2 = constants.ISO3_BY_ISO2

    missing = []
    for country in roster:
        iso2 = str(country["iso2"]).strip()
        if not iso2:
            continue
        if not _has_country_partition(root, iso2):
            missing.append(iso2)

    if not missing:
        print(f"All {len(roster)} countries already have parquet partitions in {root}.")
        return

    print(f"Backfilling {len(missing)} missing panels → {missing}")
    for iso2 in missing:
        try:
            panel = fetch_metrics.build_country_panel(
                iso2,
                indicators,
                start=start,
                end=end,
                tidy_fetch=True,
            )

            # Merge non-WB indicators (e.g. Political Corruption Index from OWID)
            panel = country_data_fetch.merge_extra_indicators(panel, iso2, iso3_by_iso2)

            if panel is None or panel.empty:
                print(f"[{iso2}] No rows for selected indicators — skipping write.")
                continue

            country_data_fetch.ingest_panel_wide(panel, iso2, root)
            print(f"[{iso2}] Wrote panel with {panel.shape[0]} years × {panel.shape[1]} indicators.")
        except Exception as e:
            print(f"[{iso2}] ERROR while backfilling panel: {e}")

# --- Main -------------------------------------------------------------------
def _series_rows(iso2: str, econ: Dict) -> List[Dict]:
    """Every resolved indicator's full history, shaped for `indicator_series`.

    The whole five-year window rather than only the latest value: a level says
    almost nothing on its own, and storing only the newest point would make the
    trajectory unrecoverable from the record.
    """
    rows: List[Dict] = []
    for ledger in econ["ledgers"].values():
        for ind in ledger["indicators"]:
            for point in ind.get("history") or []:
                if point.get("value") in (None, "unknown"):
                    continue
                rows.append({
                    "country_iso2": iso2,
                    "indicator_code": ind["code"],
                    "period": point["year"],
                    "as_of": ind["as_of"],
                    "as_of_scheme": ind["as_of_scheme"],
                    "value": point["value"],
                    "unit": ind.get("unit"),
                    "source": ind["source"],
                    "ledger": ind.get("ledger") or _ledger_of(ind["code"]),
                    "freq": "A",
                })
    return rows


def _ledger_of(code: str) -> str:
    return (constants.INDICATOR_REGISTRY.get(code) or {}).get("ledger", "")


def _imf_series_rows(iso2: str, recent: Dict) -> List[Dict]:
    """Sub-annual IMF observations, shaped for `indicator_series`."""
    rows: List[Dict] = []
    for name, d in (recent or {}).items():
        if not isinstance(d, dict) or d.get("value") is None or not d.get("period"):
            continue
        code = constants.CODE_BY_LABEL.get(name)
        if not code:
            continue
        spec = constants.INDICATOR_REGISTRY.get(code) or {}
        rows.append({
            "country_iso2": iso2,
            "indicator_code": code,
            "period": d["period"].year,
            "as_of": d["period"],
            "as_of_scheme": vintage.SOURCE_PUBLISHED,
            "value": d["value"],
            "unit": d.get("unit") or spec.get("unit"),
            "source": d.get("source") or spec.get("source", "IMF"),
            "ledger": spec.get("ledger", ""),
            "freq": d.get("freq", "M"),
        })
    return rows


# The scoring model, named once. Four call sites used to carry this string
# and one of them had already drifted to the undated alias.
SCORING_MODEL = "gpt-4o-2024-08-06"


def seed_roster() -> int:
    """Write the roster into ``country`` and read it back, returning the row count.

    The readback is the point. A seed that silently wrote nothing would otherwise
    surface much later as a foreign-key error against a country nobody was
    looking at, so the run asserts that what the roster promised actually arrived
    rather than trusting that the write returned without raising.

    Raises:
        RuntimeError: if any roster country is not readable afterwards.
    """
    facts = structural_facts.load()
    enriched = []
    for c in constants.COUNTRY_ROSTER:
        f = facts.get(c["iso2"], {})
        enriched.append({
            "iso2": c["iso2"], "iso3": c["iso3"], "name": c["name"],
            # What retrieval actually asks for, stored rather than left as a
            # constant only the fetcher knows about.
            "query_name": core.query_name(c["iso2"], c["name"]),
            "tier": c["tier"], "lat": c["lat"], "lng": c["lng"],
            "region": f.get("region"), "income_group": f.get("income_group"),
            "monetary_regime": f.get("regime"),
            "monetary_sovereignty": f.get("sovereignty"),
        })

    written = data_push.upsert_countries(enriched)
    seeded = data_push.read_countries()
    missing = [c["iso2"] for c in constants.COUNTRY_ROSTER if c["iso2"] not in seeded]
    if missing:
        raise RuntimeError(
            f"country seed did not reach the database: {', '.join(missing)}"
        )
    print(f"[roster] seeded {written} countries, {len(seeded)} readable")
    return written


def run_etl(
    only: Optional[List[str]] = None,
    dump_dir: Optional[pathlib.Path] = None,
) -> None:
    """Loop countries → payload → news → gate → digests → score → census → DB.

    Args:
        only: ISO-2 codes to run instead of the whole roster. For verification
            and for re-running one country after a failure; the scheduled run
            passes nothing and covers everything.
        dump_dir: If set, write each country's serialized payload here as it is
            sent. Verification reads these rather than rebuilding the payload
            from stored parts — a reconstruction can agree with itself and still
            differ from what the model actually received.
    """
    roster = [c for c in constants.COUNTRY_ROSTER
              if not only or c["iso2"] in {x.upper() for x in only}]
    if only:
        print(f"[run] limited to {[c['iso2'] for c in roster]}")
    print(f"=== AI Country Risk run started at {_to_utc_iso(datetime.now(timezone.utc))} UTC ===")

    # Every paid call in this run is metered, so the run can say what it cost
    # rather than leaving it to the invoice.
    run_meter = usage.Meter()

    # ONE date for the whole run, read once. Reading the clock per country puts
    # a run that crosses midnight UTC on two dates — the 48-country pass on
    # 2026-09-22 split 34/14 across two days, and because the census and the
    # snapshot each read the clock separately they disagreed by one country,
    # leaving that country's score with no census to join to. A weekly rating is
    # a statement about a week, not about the minute its row was written.
    run_as_of = datetime.now(timezone.utc).date()
    run_git_sha = provenance.git_sha()
    db.announce()
    print(f"[run] stamping every row for this run as_of={run_as_of}")

    # The run-level row is opened before any work and closed at the end. Written
    # only at the end, an interrupted run leaves nothing at run level at all:
    # the 2026-09-23 week-one run left nine per-country error rows and no sign
    # that the run itself never finished. A row still 'running' says so.
    try:
        store.write_ledger(
            job_type="run", run_date=run_as_of, status="running",
            started_at=datetime.now(timezone.utc), git_sha=run_git_sha,
            detail={"only": sorted(c["iso2"] for c in roster) if only else None},
        )
    except Exception as e:
        print(f"[ledger] run start not recorded: {e}")

    # 0a) Seed the roster into `country`. It has to come first: every other
    #     table's foreign key points at it.
    seed_roster()

    # 0b) Provision the rest of the schema. `ensure_schema` verifies after it
    #     provisions, because a CREATE TABLE IF NOT EXISTS that returns without
    #     raising is not evidence that the table is there.
    store.ensure_schema()

    # 0) Ensure/Backfill panels per country (incremental, idempotent)
    #    World Bank indicators are fetched per-country; non-WB indicators
    #    (Political Corruption Index) are merged in via merge_extra_indicators.
    ensure_missing_country_panels(
        root=PROCESSED_DATA,
        indicators=constants.INDICATORS,
        start=None,
        end=None,
        roster=roster,
    )

    # 0b) Economic calendar (FMP) for the front-end Econ Calendar pane. Guarded
    #     so a calendar failure never aborts the country/risk loop below.
    try:
        events = fmp_calendar_fetch.fetch_economic_calendar()
        if events:
            # AI-rank the next-14-day subset by importance to investors (US-tilted).
            # Failure here must not block the upsert, so it is guarded separately.
            cutoff = datetime.now(timezone.utc) + timedelta(days=constants.CAL_RANK_HORIZON_DAYS)
            subset = [ev for ev in events if ev["event_time"] <= cutoff]
            for i, ev in enumerate(subset, start=1):
                ev["_rank_id"] = f"e{i}"
            try:
                scores = calendar_ranker.rank_calendar_events(subset)
                scored_at = datetime.now(timezone.utc)
                for ev in subset:
                    s = scores.get(ev.get("_rank_id"))
                    if s:
                        ev["ai_importance"] = s.get("importance")
                        ev["ai_rationale"]  = s.get("rationale")
                        ev["ai_scored_at"]  = scored_at
                print(f"[econ-calendar] AI-ranked {len(scores)}/{len(subset)} next-14d events")
            except Exception as rank_err:
                print(f"[econ-calendar] ranking ERROR: {rank_err}")

            data_push.upsert_economic_events(events)
            print(f"[econ-calendar] upserted {len(events)} events")
        else:
            print("[econ-calendar] no events fetched (skipping upsert)")
    except Exception as e:
        print(f"[econ-calendar] ERROR: {e}")

    # 0c) Refresh fast-moving indicators (Inflation) from the IMF at monthly
    #     frequency into `indicator_series`. World Bank values are annual and lag
    #     1–2 years, so the sub-annual observation sits beside them at its own
    #     period. Guarded per-country so an IMF gap or outage never blocks
    #     the risk loop below.
    if constants.IMF_RECENT_INDICATORS:
        refreshed = 0
        for c in roster:
            try:
                recent = imf_macro_fetch.fetch_recent_indicators(c["iso3"])
                if recent:
                    # Sub-annual observations join the same series as everything
                    # else. `recent_indicator` held one row per pair, overwritten
                    # in place, which made it useless for anything but "latest".
                    store.upsert_indicator_series(
                        _imf_series_rows(c["iso2"], recent)
                    )
                    refreshed += 1
            except Exception as e:
                print(f"[imf-refresh] {c['iso2']} ERROR: {e}")
        print(f"[imf-refresh] refreshed {refreshed}/{len(roster)} countries")

    # Map "Country_Name" → "iso2" from the hardcoded roster
    country_map = {c["name"]: c["iso2"] for c in roster}

    # Pool every country's Top-3 articles for the post-loop global alert ranking.
    global_alert_pool: List[Dict] = []

    # Per-country wall-clock and spend. This is the weekly run's real cost, and
    # it should be on the record from the first full pass rather than estimated
    # later from an invoice.
    per_country: List[Dict] = []

    for country_name, iso2 in country_map.items():
        started = time.monotonic()
        started_wall = datetime.now(timezone.utc)
        spend_before = run_meter.spend_usd
        tokens_before = (run_meter.input_tokens, run_meter.output_tokens)
        outcome = "ok"
        try:
            # 1) Macro payload (pretty, JSON-serializable). ALL_INDICATORS adds
            #    the merged non-WB indicators (Political Corruption Index) so they
            #    reach both the LLM payload and the DB upsert.
            payload = data_retrieval.prepare_llm_payload_pretty(
                country_iso=iso2,
                indicators=constants.ALL_INDICATORS,
                since=2015,
                lookback=10,
                deltas=(1, 5),
            )

            # 1b) The economics block: the same numbers, grouped by ledger, each
            #     carrying five years of history with the direction stated in
            #     words, and the date it became knowable rather than the date we
            #     fetched it. A ledger that resolved nothing says so here and is
            #     counted below.
            econ = payload_builder.build_economics_block(
                data_retrieval.panel_values(iso2, lookback=10),
                curated_loader.load_for_country(iso2),
                today=run_as_of,
                first_seen=store.read_first_seen(iso2),
            )
            res = econ["resolution"]
            print(
                f"[econ] {iso2}: resolved {res['resolved_by_ledger']} of "
                f"{res['expected_by_ledger']} | dates {res['as_of_schemes']}"
            )
            if res["empty_ledgers"]:
                print(
                    f"[econ] {iso2}: LEDGER WITH NO INDICATORS: "
                    f"{', '.join(res['empty_ledgers'])} — "
                    f"dropped: {[d['code'] + ' (' + d['reason'] + ')' for d in res['dropped']]}"
                )

            # 2) Fetch relevant news using multi-query strategy with relevance filtering (+ BROAD query)
            candidates, pool_report = _fetch_candidate_pool(country_name or iso2, iso2)

            # 2b) The relevance gate. Every candidate is classified once, for
            #     this country, before anything expensive touches it. Only
            #     `relevant` articles are eligible, and nothing tops up from
            #     the rest: if six qualify, six are scored. In `body` mode a
            #     body a previous digest called `not_article` is read as a
            #     snippet; the cache is the only digest available this early.
            if relevance.DEFAULT_INPUT_MODE == "body":
                known = digest_engine.cached_body_quality(candidates)
                for c in candidates:
                    q = known.get(c.get("publisher_link") or c.get("link"))
                    if q:
                        c["body_quality"] = q
            labels = relevance.classify(
                candidates, country_name or iso2, iso2, meter=run_meter
            )
            gate = relevance.select(candidates, labels, iso2)
            items = gate["selected"]
            counts = gate["counts"]
            print(
                f"[gate] {iso2}: {counts['candidates']} candidates -> "
                f"{counts['eligible']} relevant -> {counts['selected']} selected "
                f"(budget {counts['budget']}); rejected {counts['rejected_by_label']}"
            )
            print(f"[gate] {iso2}: per-ledger {gate['per_ledger']} | per-theme {gate['per_theme']}")
            dupes = relevance.duplicate_story_report(items)
            if dupes["duplicated_slots"]:
                print(
                    f"[gate] {iso2}: {dupes['duplicated_slots']} of {len(items)} slots "
                    f"hold a story another selected article also tells"
                )

            # 2c) Stage one: digest every admitted article, then choose the few
            #     to read in full. Breadth from the digests, depth from three —
            #     twenty full bodies is unaffordable and twenty headlines throws
            #     away the reporting already paid for.
            #
            #     The digest comes first because it says what each body is. Only
            #     a body the digest calls `full` can be read in full, so the
            #     three full reads go to the first three such articles in the
            #     gate's order. A `partial` body (the article, cut off by a
            #     wall) is digest-only; a `not_article` body (the wall itself)
            #     is title-only and its digest is not sent. An article whose
            #     digest failed is not vouched for, so it is not read in full.
            digested = digest_engine.digest_articles(items, meter=run_meter)
            full_reads = 0
            for it in items:
                key = it.get("publisher_link") or it.get("link")
                digest = digested["digests"].get(key)
                quality = (digest or {}).get("body_quality") if (it.get("text") or "").strip() else None
                it["body_quality"] = quality
                it["digest"] = None if quality == "not_article" else digest
                reads_full = quality == "full" and full_reads < relevance.FULL_TEXT_K
                status, clipped, original = digest_engine.body_status_for(
                    it, full_text=reads_full, quality=quality
                )
                it["body_status"] = status
                it["body_clipped"] = clipped
                it["body_chars_original"] = original
                full_reads += status in ("full", "clipped")

            # Keep the evidence. Without it a score is unauditable after the
            # fact and last week's scoring cannot be re-run on what it saw.
            try:
                store.upsert_articles(_article_rows(candidates))
            except Exception as e:
                print(f"[{iso2}] could not store articles: {e}")

            dc = digested["counts"]
            status_mix: Dict[str, int] = {}
            quality_mix: Dict[str, int] = {}
            for it in items:
                status_mix[it["body_status"]] = status_mix.get(it["body_status"], 0) + 1
                if (it.get("text") or "").strip():   # no body is counted as no_body
                    q = it["body_quality"] or "unassessed"
                    quality_mix[q] = quality_mix.get(q, 0) + 1
            print(
                f"[digest] {iso2}: {dc['generated']} generated, {dc['cached']} cached, "
                f"{dc['truncated_retry']} truncated-retry, {dc['failed']} failed, "
                f"{dc['no_body']} without a body | bodies {status_mix} | quality {quality_mix}"
            )
            pool_report["digests"] = dc
            pool_report["body_status"] = status_mix

            pool_report["gate"] = counts
            pool_report["per_ledger"] = gate["per_ledger"]
            pool_report["rejected"] = gate["rejected"]
            pool_report["duplicate_slots"] = dupes["duplicated_slots"]

            # --- Resolve and do light enrichment using ONLY the simple scraper ---
            with requests.Session() as _sess:
                # a) Replace news.google.com wrappers with publisher URLs
                for it in items:
                    link = it.get("link")
                    if isinstance(link, str) and "news.google.com" in link:
                        it["link"] = resolve_google_news_url(link, session=_sess)

                # a2) Defense-in-depth: drop denylisted sources now that links are
                #     resolved, in case a wrapper couldn't be resolved earlier.
                before = len(items)
                items = [it for it in items if not is_blocked_url(it.get("link"))]
                removed = before - len(items)
                if removed:
                    print(f"[{iso2}] Blocked {removed} article(s) from denylisted sources.")

                # b) Ensure summary/content and thumbnail (simple scraper, single GET)
                for it in items:
                    link = it.get("link")
                    if not isinstance(link, str) or not link.startswith("http"):
                        continue

                    cur_sum = (it.get("summary") or "").strip()
                    source  = (it.get("source")  or "").strip()
                    need_summary = (not cur_sum) or (len(cur_sum.split()) < 8) or (cur_sum.lower() == source.lower())
                    need_image = not it.get("image")

                    if need_summary or need_image:
                        thumb, summary, full_text = get_article_assets(link, session=_sess, max_words=160)
                        if need_summary and summary:
                            it["summary"] = summary
                        if full_text:
                            it["content"] = full_text[:24000]
                        if need_image and thumb:
                            it["image"] = thumb

            # 2d) The census: what the registry promised against what reached
            #      the model. Stored, and read back — a census nobody stores
            #      cannot be compared against last week, and comparison is the
            #      only way to tell a source that broke from a quiet country.
            census = payload_health.build_census(
                iso2,
                run_as_of,
                economics=econ,
                pool_report=pool_report,
                gate=gate,
                digests=dc,
                versions=provenance.run_versions(
                    scoring_model=SCORING_MODEL,
                    digest_model=digest_engine.DEFAULT_MODEL,
                    gate_model=relevance.DEFAULT_MODEL,
                    prompt_version=ai_constants.PROMPT_VERSION,
                    digest_prompt_version=digest_engine.DIGEST_PROMPT_VERSION,
                    relevance_prompt_version=relevance.RELEVANCE_PROMPT_VERSION,
                    seed=42,
                    extra={
                        "gate_input_mode": relevance.DEFAULT_INPUT_MODE,
                        "exposure_cards_version": relevance.EXPOSURE_CARDS_VERSION,
                        "gate_cache_version": relevance.cache_version(
                            relevance.DEFAULT_MODEL, relevance.DEFAULT_INPUT_MODE
                        ),
                    },
                ),
                budget=relevance.ARTICLE_BUDGET,
            )
            print(payload_health.format_census(census))

            # The census travels inside the snapshot's manifest rather than in a
            # table of its own that nothing joined to. Comparison against the
            # country's own history is the quality report's job, at the end of
            # the run, where it can be read as a block instead of scrolling past
            # one country at a time.

            # The macro half of the evidence, kept as a series. `as_of` is in
            # the key, so a revision arrives as a new row and this week's score
            # stays re-readable against the numbers as they stood.
            try:
                store.upsert_indicator_series(_series_rows(iso2, econ))
            except Exception as e:
                print(f"[series] {iso2} could not store indicators: {e}")

            # Assign stable ids ("a1","a2",...)
            for i, it in enumerate(items, start=1):
                it["id"] = f"a{i}"

            # 3) Assemble the payload and score.
            #    Structural facts first, so everything after is read against
            #    them; then the economics by ledger, the theme counts, the
            #    digests, the top-k full texts, and the computed coverage.
            scoring_payload = payload_builder.build_scoring_payload(
                iso2,
                country_name,
                structural=structural_facts.for_country(iso2),
                economics=econ,
                pool_report=pool_report,
                gate=gate,
                coverage=census["coverage_components"],
                full_text_k=relevance.FULL_TEXT_K,
                body_cap_chars=digest_engine.BODY_CAP_CHARS,
            )
            article_ids = [a["id"] for a in scoring_payload["articles"]]

            # What the self-hosted scorer would have to serve, measured on the
            # compact JSON that actually goes out rather than on a pretty-printed
            # copy, which would overstate it by about a third.
            payload_tokens = payload_builder.count_tokens(
                json.dumps(scoring_payload, ensure_ascii=False, default=str)
            )["tokens"]
            census["versions"]["payload_tokens"] = payload_tokens

            if dump_dir is not None:
                # Exactly the bytes the model is about to be handed.
                dump_dir.mkdir(parents=True, exist_ok=True)
                (dump_dir / f"{iso2}-payload.json").write_text(
                    json.dumps(scoring_payload, indent=2, ensure_ascii=False,
                               default=str),
                    encoding="utf-8",
                )
                (dump_dir / f"{iso2}-census.json").write_text(
                    json.dumps(census, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )

            scored = langchain_llm.score_country(
                iso2=iso2,
                payload=scoring_payload,
                article_ids=article_ids,
                model=SCORING_MODEL,
                seed=42,
                meter=run_meter,
            )
            if scored["failed"] or not scored["answer"]:
                raise RuntimeError(f"scoring failed: {scored['failed']}")

            answer = scored["answer"]
            violations = scored["violations"]
            if violations:
                # Counted rather than swallowed. A run where the model returned
                # three out-of-range scores and a clamp quietly fixed them is a
                # different run from one where it did not.
                print(f"[score] {iso2}: {len(violations)} schema violation(s): "
                      f"{[v['field'] + ':' + v['problem'] for v in violations][:6]}")
                census["versions"]["schema_violations"] = len(violations)
                census["versions"]["violations"] = violations

            if scored["badge"]:
                # Observation only. The rating stays the model's own; the legal
                # fact sits beside it.
                print(f"[score] {iso2}: RESTRICTED — {scored['badge']['rule'][:90]}")

            # 4) The top three are the articles the model read in full — the
            #    first three the digest called `full`, in the gate's own order —
            #    so what the dashboard shows and what the score was made from
            #    cannot diverge. If fewer than three were read in full, the
            #    rest come next in gate order; a wall is never one of them.
            bearing_by_id = {
                a["id"]: a.get("bearing")
                for a in answer["article_scores"]
                if a.get("id")
            }
            top_items = [it for it in items if it["body_status"] in ("full", "clipped")]
            top_items += [it for it in items if it not in top_items
                          and it["body_quality"] != "not_article"]
            top_items = top_items[:3]

            # 5) Enrich ONLY the Top-3 with missing images using the advanced scraper
            cb_token = _crawlbase_token()
            if cb_token:
                for it in top_items:
                    if it.get("image"):
                        continue
                    link = it.get("publisher_link") or it.get("link") or ""
                    if not isinstance(link, str) or not link.startswith("http"):
                        continue
                    rec = crawlbase_scrape_one(link, cb_token, respect_robots=True)
                    if rec.get("error") or rec.get("skipped"):
                        continue
                    if rec.get("image_url"):
                        it["image"] = rec["image_url"]
                    if (not it.get("published")) and rec.get("published_at"):
                        it["published"] = rec["published_at"]

            # 6) Build Top-3 payload AFTER enrichment
            top_articles = []
            for r, it in enumerate(top_items, start=1):
                top_articles.append({
                    "rank": r,
                    "id": it.get("id"),
                    "url": it.get("publisher_link") or it.get("link") or "",
                    "title": it.get("title") or "",
                    "source": it.get("source") or "",
                    "published_at": it.get("page_published_at") or it.get("published") or None,
                    "impact": bearing_by_id.get(it.get("id")),
                    "summary": it.get("summary") or it.get("snippet") or "",
                    "image": it.get("image"),
                })

            # 6b) Add this country's Top-3 to the global alert pool (ranked after the loop)
            for a in top_articles:
                global_alert_pool.append({**a, "country_iso2": iso2, "country_name": country_name})

            # 7) Write the week down. The manifest is what makes this row
            #    comparable with next week's, and `store.upsert_snapshot`
            #    refuses a row without one.
            versions = census["versions"]
            store.upsert_snapshot({
                "country_iso2": iso2,
                "run_date": run_as_of,
                "score_12m": answer["score_12m"],
                "score_3m": answer["score_3m"],
                "friction_score": answer.get("friction"),
                "order_score": answer.get("order"),
                "information_score": answer.get("information"),
                "edge_score": answer.get("edge"),
                "condition_flags": answer.get("condition_flags") or {},
                "bullet_summary": answer.get("bullet_summary"),
                "subscore_evidence": answer.get("subscore_evidence") or {},
                "article_scores": answer.get("article_scores") or [],
                "top_articles": top_articles,
                "evidence_coverage": census["evidence_coverage"],
                "coverage_components": census["coverage_components"],
                "payload_fingerprint": census["payload_fingerprint"],
                "prompt_version": versions.get("prompt_version"),
                "scoring_model": versions.get("scoring_model"),
                "digest_model": versions.get("digest_model"),
                "gate_model": versions.get("gate_model"),
                "seed": versions.get("seed"),
                "git_sha": versions.get("git_sha"),
                "payload_tokens": payload_tokens,
                "restricted_badge": scored["badge"],
                "manifest": {
                    "census": census,
                    "selected": [
                        {"id": a.get("id"),
                         "url": a.get("publisher_link") or a.get("link"),
                         "content_sha256": _body_hash(a),
                         "body_status": a.get("body_status"),
                         "body_quality": a.get("body_quality"),
                         "themes": a.get("themes") or []}
                        for a in items
                    ],
                    "rejected": gate["rejected"],
                    "indicators": [
                        {"code": i["code"], "period": i["period"],
                         "as_of": i["as_of"], "as_of_scheme": i["as_of_scheme"]}
                        for led in econ["ledgers"].values()
                        for i in led["indicators"]
                    ],
                    "per_ledger_articles": gate["per_ledger"],
                    "payload_tokens": payload_tokens,
                },
            })

            print(
                f"[{iso2}] score_12m={answer['score_12m']} score_3m={answer['score_3m']} "
                f"ledgers friction={answer['friction']} order={answer['order']} "
                f"information={answer['information']} edge={answer['edge']} "
                f"coverage={census['evidence_coverage']}"
            )

        except Exception as e:
            outcome = f"ERROR: {e}"
            print(f"[{iso2}] ERROR: {e}")

        elapsed = time.monotonic() - started
        spent = run_meter.spend_usd - spend_before
        per_country.append(
            {"iso2": iso2, "seconds": round(elapsed, 1),
             "usd": round(spent, 4), "outcome": outcome}
        )
        print(f"[time] {iso2}: {elapsed:.1f}s, ${spent:.4f}")

        # One ledger row per unit of work, carrying where it ran and what it
        # cost. A weekly job nobody can bill or locate is one nobody can debug.
        try:
            store.write_ledger(
                job_type="etl", country_iso2=iso2, run_date=run_as_of,
                started_at=started_wall,
                finished_at=datetime.now(timezone.utc),
                status="ok" if outcome == "ok" else "error",
                git_sha=run_git_sha,
                input_tokens=run_meter.input_tokens - tokens_before[0],
                output_tokens=run_meter.output_tokens - tokens_before[1],
                spend_usd=round(spent, 6),
                detail={"seconds": round(elapsed, 1)},
                error=None if outcome == "ok" else outcome[:500],
            )
        except Exception as e:
            print(f"[ledger] {iso2} not recorded: {e}")

    # 7b) The run's own shape, printed as a table. A weekly job that cannot say
    #     what it cost or where it spent its time is one nobody can budget for.
    if per_country:
        ok = [c for c in per_country if c["outcome"] == "ok"]
        failed = [c for c in per_country if c["outcome"] != "ok"]
        total_s = sum(c["seconds"] for c in per_country)
        total_usd = sum(c["usd"] for c in per_country)
        times = sorted(c["seconds"] for c in per_country)
        median_s = times[len(times) // 2]
        print("")
        print(f"[run] {len(ok)}/{len(per_country)} countries scored, "
              f"{len(failed)} failed")
        print(f"[run] wall-clock {total_s / 60:.1f} min total, "
              f"{median_s:.1f}s median per country, "
              f"{total_s / len(per_country):.1f}s mean")
        print(f"[run] spend ${total_usd:.4f} total, "
              f"${total_usd / max(len(ok), 1):.4f} per scored country")
        slowest = sorted(per_country, key=lambda c: -c["seconds"])[:5]
        print(f"[run] slowest: "
              + ", ".join(f"{c['iso2']} {c['seconds']:.0f}s" for c in slowest))
        if failed:
            for c in failed:
                print(f"[run] FAILED {c['iso2']}: {c['outcome'][:120]}")

    # 8) Global news alerts: rank the pooled Top-3 articles by importance to the
    #    global economy and persist the top-N. Guarded so a failure here never
    #    affects the per-country snapshots already written above.
    try:
        ranked_alerts = alerts_ranker.rank_global_alerts(global_alert_pool)
        if ranked_alerts:
            data_push.upsert_news_alerts(ranked_alerts, run_date=run_as_of)
            print(f"[alerts] ranked {len(ranked_alerts)}/{len(global_alert_pool)} pooled articles, stored {len(ranked_alerts)}")
        else:
            print(f"[alerts] no alerts ranked from {len(global_alert_pool)} pooled articles (skipping upsert)")
    except Exception as e:
        print(f"[alerts] ERROR: {e}")

    # What the run actually cost, from the usage the provider reported rather
    # than from an estimate. A run that cannot say what it spent is a run whose
    # budget cap is decoration.
    print(run_meter.summary())

    try:
        store.write_ledger(
            job_type="run", run_date=run_as_of, status="ok",
            finished_at=datetime.now(timezone.utc), git_sha=run_git_sha,
            input_tokens=run_meter.input_tokens,
            output_tokens=run_meter.output_tokens,
            spend_usd=round(run_meter.spend_usd, 6),
            detail={"countries": len(per_country),
                    "failed": [c["iso2"] for c in per_country
                               if c["outcome"] != "ok"]},
        )
    except Exception as e:
        print(f"[ledger] run not recorded: {e}")

    # 9) The weekly quality report, every run. A report you have to remember to
    #    run is a report that stops being run.
    try:
        quality_report.run_report(run_as_of)
    except Exception as e:
        print(f"[report] ERROR: {e}")

    print(f"=== Run finished at {_to_utc_iso(datetime.now(timezone.utc))} UTC ===")
