"""The daily run's phases — the only place fetch, AI, and storage meet.

Every other module under ``utils/`` owns exactly one concern: ``data_fetching``
talks to upstream APIs, ``news_fetching`` gathers and ranks articles, ``ai``
calls the model, ``data_upsert`` writes Postgres. Dependencies between them
stay one-way (only ``ai.digest_engine`` reaches down into ``data_upsert``,
for its digest cache), which is what keeps a change in one from destabilizing
the rest.

The work of a run, though, is inherently cross-cutting: "fetch the calendar,
have the model rank it, store the result" touches three of those domains. That
orchestration lives here, at the top of the dependency graph, so the layers
below stay one-way. ``main.py`` calls these four functions in order and does
nothing else.

Each phase owns its own resilience boundary: a failure is logged with a full
traceback and the run continues, so one flaky upstream or one bad country
costs a single phase rather than the whole day.
"""

import logging
import os
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from backend.utils import constants, data_retrieval, lint, provenance
from backend.utils.ai import alerts_ranker, calendar_ranker, digest_engine, langchain_llm
from backend.utils.ai import client as ai_client
from backend.utils.data_fetching import (
    bis_bulk_fetch, curated_loader, fmp_calendar_fetch, imf_macro_fetch, wb_series_fetch,
)
from backend.utils.data_upsert import data_push
from backend.utils.masking import gazetteer, probe, rewrite
from backend.utils.news_fetching import article_enrichment, article_ranking

logger = logging.getLogger(__name__)

# Macro payload window handed to the panel/DB payload.
_PAYLOAD_SINCE_YEAR = 2015
_PAYLOAD_LOOKBACK_YEARS = 10
_PAYLOAD_DELTA_HORIZONS = (1, 5)

# Articles fetched per country before Top-3 selection.
_MAX_ARTICLES_PER_COUNTRY = 20


def _safe(read: Callable[[], Any], iso2: str, what: str) -> Any:
    """Run one evidence read, degrading a failure to None with a warning.

    Evidence is additive: a country missing its series store still scores on its
    panel, and one that lost every store still scores on its articles. Only the
    absence of an assessment is fatal, and that is decided by the model call.
    """
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - any read failure degrades to absent
        logger.warning("[%s] %s unavailable: %s", iso2, what, exc)
        return None


def _to_100(value: Optional[float]) -> Optional[int]:
    """Convert a stored 0-1 score back to the 0-100 grid lint's tripwires use."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


def _rewrite_fulltext(items: List[Dict], fulltext_ids: List[str], iso2: str,
                      cache: Optional[Any] = None) -> None:
    """Model-mask the handful of bodies the scorer reads end to end. Mutates.

    The gazetteer masks what somebody wrote down. It does not know this week's
    finance minister, this year's ruling party, or the bank that just failed —
    and those are named in the full text far more often than the country is.

    Fails **closed**: a rewrite that errors or comes back empty leaves the
    article with no body, so it reaches the scorer as its masked title. Being
    short one body costs a week some evidence; one leaked name costs the whole
    comparison.

    Args:
        cache: an optional store keyed on content hash, consulted before the
            model and written after it. The daily run passes None and behaves
            exactly as before; a backfill passes one for two reasons.

            The cheap one is overlap: weekly anchors across a 30-day window put
            the same article in about four consecutive snapshots, and a
            top-severity article stays top-severity in all four, so the same
            body was being rewritten four times.

            The one that matters is reproducibility. `input_manifest` hashes the
            bytes the model read, and for these articles those bytes were
            generated prose kept nowhere — so a rebuild produced a different
            sentence and a different hash, and the manifest's promise failed on
            exactly the three articles the scorer weighted most heavily.
    """
    if not fulltext_ids:
        return
    by_id = {it.get("id"): it for it in items if isinstance(it, dict)}
    targets = [(aid, by_id[aid]) for aid in fulltext_ids
               if by_id.get(aid) and by_id[aid].get("text")]
    if not targets:
        return
    # The key is checked per miss rather than up front, so a fully cached
    # snapshot needs no key at all. That is what lets `rebuild_snapshot` re-derive
    # a stored row for free — and a miss during a rebuild is the finding, not an
    # inconvenience.
    api_key = os.getenv("OPENAI_API_KEY")

    # The hash is over the masked body, which is what the model is handed and
    # what a rebuild will re-derive. A gazetteer change therefore lands in the
    # key without the gazetteer version being part of it.
    shas = {aid: provenance.text_sha256(item["text"]) for aid, item in targets}
    hits: Dict[str, str] = {}
    if cache is not None:
        try:
            hits = cache.read_rewrite_cache(
                sorted(set(shas.values())), rewrite.REWRITE_VERSION, "masked")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] rewrite cache read failed (%s); rewriting all",
                           iso2, exc)

    fresh: List[Dict[str, str]] = []
    for aid, item in targets:
        cached = hits.get(shas[aid])
        if cached:
            item["text"] = cached
            continue
        if not api_key:
            # Same fail-closed rule as a failed rewrite: an unmasked body must
            # not reach the scorer just because there was no key to mask it.
            logger.warning("[%s] %s degraded to title-only: no OPENAI_API_KEY "
                           "and no cached rewrite", iso2, aid)
            item["text"] = ""
            continue
        item["text"] = rewrite.rewrite_body(item["text"], api_key)
        if not item["text"]:
            logger.warning("[%s] %s degraded to title-only: the mask rewrite "
                           "would not clear its body", iso2, aid)
            continue
        fresh.append({"content_sha256": shas[aid], "rewritten": item["text"]})

    logger.info("[%s] full-text rewrites: %d cached, %d fresh",
                iso2, len(targets) - len(fresh), len(fresh))
    if fresh and cache is not None:
        try:
            cache.write_rewrite_cache(fresh, rewrite.REWRITE_VERSION, "masked")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] rewrite cache write failed: %s", iso2, exc)


# How often the live run asks the cheap model to guess which country it is
# looking at. One country in six, so the whole roster is sampled about weekly at
# a daily cadence, for a few cents a month.
#
# It runs in production rather than only in the pilot because identifiability is
# not a property of the method, it is a property of *this week's evidence*. A
# quiet week masks well and a week where the only story is a named central bank
# governor does not, and the only way to know which kind of week the series is
# accumulating is to keep measuring. A one-off experiment answers the question
# once, for a corpus nobody will score again.
_PROBE_EVERY_NTH_COUNTRY = 6


def _identifiability(items: List[Dict], iso2: str, as_of: date,
                     fulltext_ids: Optional[List[str]] = None) -> Optional[Dict]:
    """Ask the cheap model which country this masked bundle is about, sometimes.

    Returns None when this country is not in today's sample, which is most of
    them. The result is a measurement and never a gate: unlike ``assert_clean``,
    a confident correct guess does not stop the snapshot. It could not — the
    US is expected to be identified nearly always, from coverage volume alone,
    and refusing to score the US would be answering the wrong question.
    """
    # Deterministic on (country, date) rather than random, so re-running a day
    # probes the same countries and the meter is reproducible. Not `hash()`:
    # Python salts string hashing per process, so that would have re-sampled on
    # every restart while the comment claimed otherwise.
    seed = zlib.crc32(f"{iso2}:{as_of.toordinal()}".encode())
    if seed % _PROBE_EVERY_NTH_COUNTRY != 0:
        return None
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    # Masked again, because the digest is generated *after* the first pass and
    # `country_llm_score` masks everything a second time on its way out. Probing
    # `items` as they stand here reads digests in a state the model never sees,
    # and the meter would have reported the instrument leakier than it is.
    guess = probe.probe(rewrite.mask_items(items, iso2), api_key,
                        fulltext_ids=fulltext_ids)
    logger.info("[%s] identifiability probe: guessed %s at %.2f",
                iso2, guess.get("country"), guess.get("confidence", 0.0))
    # Stored as well as returned. The manifest copy is per snapshot and only
    # exists where a snapshot does; this one is queryable across masking
    # versions, which is what a re-probe needs to diff against. The last probe
    # run left its results in a commit message and six incidental cache rows.
    data_push.upsert_probe_result(
        iso2, as_of, guess,
        mask_map_version=gazetteer.MASK_MAP_VERSION,
        sweep_version=rewrite.SWEEP_VERSION,
        probe_model=ai_client.DIGEST_MODEL_NAME,
        n_articles=len(items),
    )
    return guess


def refresh_calendar() -> None:
    """Fetch the FMP economic calendar, AI-rank the near term, and upsert.

    The AI ranking is guarded separately from the upsert: a ranking failure
    still leaves the raw events stored (unranked), which the front-end renders
    fine. Any other failure is logged and swallowed so the country loop runs.
    """
    try:
        events = fmp_calendar_fetch.fetch_economic_calendar()
        if not events:
            logger.info("[econ-calendar] no events fetched (skipping upsert)")
            return

        # AI-rank the next-14-day subset by importance to investors (US-tilted).
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
            logger.info("[econ-calendar] AI-ranked %d/%d next-14d events", len(scores), len(subset))
        except Exception:
            logger.exception("[econ-calendar] ranking ERROR")

        data_push.upsert_economic_events(events)
        logger.info("[econ-calendar] upserted %d events", len(events))
    except Exception:
        logger.exception("[econ-calendar] ERROR")


def refresh_imf_indicators() -> None:
    """Refresh fast-moving indicators (Inflation) from the IMF into
    ``recent_indicator``.

    World Bank values are annual and lag 1-2 years; the front-end prefers this
    fresher monthly value and falls back to the WB annual one when a country
    has no IMF observation. Guarded per-country so an IMF gap or outage never
    blocks the risk loop. No-op when no indicators are configured.
    """
    if not constants.IMF_RECENT_INDICATORS:
        return

    refreshed = 0
    for c in constants.COUNTRY_ROSTER:
        try:
            recent = imf_macro_fetch.fetch_recent_indicators(c["iso3"])
            if recent:
                data_push.upsert_recent_indicators(c["iso2"], recent)
                refreshed += 1
            # The same source as a full history, for the volatility windows.
            # `recent_indicator` holds one row per country and cannot carry it.
            rows = imf_macro_fetch.fetch_series_rows(c["iso2"], c["iso3"])
            if rows:
                data_push.upsert_indicator_series(rows)
        except Exception:
            logger.exception("[imf-refresh] %s ERROR", c["iso2"])
    logger.info("[imf-refresh] refreshed %d/%d countries", refreshed, len(constants.COUNTRY_ROSTER))


def refresh_ledger_sources() -> None:
    """Refresh the friction framework's own sources into ``indicator_series``.

    Three independent upstreams — the extra World Bank annual codes, the BIS
    bulk files (policy rates and USD exchange rates), and the curated drop
    folder. Each is guarded separately: BIS being down must not cost the run its
    World Bank series, and a typo in one curated file must not cost it either.

    Run once per day before the country loop, since every source is a
    whole-roster fetch rather than a per-country one.
    """
    for label, refresh in (
        ("wb-series", wb_series_fetch.refresh_wb_series),
        ("bis", bis_bulk_fetch.refresh_bis_series),
    ):
        try:
            refresh()
        except Exception:
            logger.exception("[%s] ERROR", label)

    try:
        rows = curated_loader.load_curated_series()
        if rows:
            data_push.upsert_indicator_series(rows)
        logger.info("[curated] loaded %d series row(s)", len(rows))
    except Exception:
        # Loud on purpose in the loader; isolated here so a malformed curated
        # file is surfaced without costing the run its scores.
        logger.exception("[curated] series load ERROR")


def _process_country(country_name: str, iso2: str, global_alert_pool: List[Dict],
                     *, as_of: Optional[date] = None,
                     items: Optional[List[Dict]] = None,
                     scoring_mode: str = "masked",
                     upsert: bool = True,
                     digest_content_cache: Optional[Any] = None) -> tuple:
    """Run the full pipeline for one country: macro payload → news → LLM score
    → Top-3 selection/enrichment → DB upsert. Appends the country's Top-3 to
    ``global_alert_pool`` for the post-loop global alert ranking.

    Args:
        as_of: pin the snapshot to a past date instead of today. Every stage
            below already takes ``as_of`` as a real parameter — digests, the
            evidence payload, the prompt, the sanctions lookup, the upsert key —
            so pinning ``_meta.generated_at``, the single place the date is
            derived from, pins all of them at once.
        items: pre-assembled articles, from ``history.snapshot_select``. Supplying
            them is the *only* difference between a historical run and a live
            one: the article source changes and nothing else does.
        scoring_mode: which regime scored this row.

            ``'masked'`` is the default and the production regime: the model is
            shown the evidence with the identity removed and every number
            intact. Backfilling 2016 and scoring tomorrow have to be the same
            instrument, and the only way that is true is if the live run and the
            backfill present the model with the same anonymized structure.

            ``'named'`` is the diagnostic twin, for measuring what identity was
            worth.

            ``'masked_nostructural'`` is the same masked payload with the
            ``structural`` block withheld. It exists because divergence between
            masked and named is ambiguous on its own: a small gap could mean the
            structural facts recovered what the name carried, or that the name
            never mattered. Only the third arm separates those.
        upsert: write to ``risk_snapshot``. False for the diagnostic modes,
            which land in ``history_run_ledger`` instead — they share
            ``(country, as_of)`` with their masked twin and would overwrite the
            production series on its own primary key.
        digest_content_cache: forwarded to ``digest_engine.digest_articles``. A
            backfill hands it ``history.store``, whose digest cache is keyed on
            content instead of on ``as_of`` and so survives the overlap between
            consecutive anchors. Forwarded rather than imported: this module sits
            above the layers, and reaching down into the backfill package to find
            a cache would invert that.

    Returns:
        ``(llm_output, input_manifest)``, so a caller that suppressed the upsert
        still has something to record.
    """
    historical = as_of is not None

    # 1) Macro payload (pretty, JSON-serializable). ALL_INDICATORS adds
    #    the merged non-WB indicators (Political Corruption Index) so they
    #    reach both the LLM payload and the DB upsert.
    payload = data_retrieval.prepare_llm_payload_pretty(
        country_iso=iso2,
        indicators=constants.ALL_INDICATORS,
        since=_PAYLOAD_SINCE_YEAR,
        lookback=_PAYLOAD_LOOKBACK_YEARS,
        deltas=_PAYLOAD_DELTA_HORIZONS,
    )

    if historical:
        # The one pin. `payload_as_of` reads this field and every downstream
        # stage takes its date from that call, so overwriting it here is what
        # makes the whole run happen on `as_of` rather than today.
        payload.setdefault("_meta", {})["generated_at"] = as_of.isoformat()

    # 2) Fetch relevant news using multi-query strategy with relevance filtering
    if items is None:
        items = article_enrichment.fetch_relevant_news(
            country_name or iso2, max_articles=_MAX_ARTICLES_PER_COUNTRY
        )

    if items:
        avg_rel = sum(it.get("relevance_score", 0) for it in items) / len(items)
        logger.info("[%s] Fetched %d articles (avg relevance: %.2f)", iso2, len(items), avg_rel)

    if not historical:
        # Resolution and body extraction are what turn a Google News wrapper
        # into an article. Historical items arrive already resolved, with the
        # body the harvest stored, so re-fetching them would replace a
        # vintage-stamped body with today's copy of the page — the exact
        # hindsight `snapshot_select` refuses.
        items = article_enrichment.resolve_and_enrich(items, iso2)

    # Assign stable ids ("a1","a2",...)
    for i, it in enumerate(items, start=1):
        it["id"] = f"a{i}"

    # 2a) Masking, and it happens here rather than at the call because a digest
    #     made from named text carries the name into the prompt however clean
    #     the article beside it is. Everything from this line to the score reads
    #     `scored`; everything the database and the front end read stays on
    #     `items`, unmasked. Masking is a transform at the scoring boundary and
    #     nowhere else — the same rule the harvest follows for stored bodies.
    masked = scoring_mode.startswith("masked")
    scored = rewrite.mask_items(items, iso2) if masked else items
    display = langchain_llm.MASKED_COUNTRY_LABEL if masked else country_name

    # 2b) Stage 1: digest every article's full text with the cheap model,
    #     keyed on the same as_of the snapshot upsert will use, then pick
    #     which articles the scorer reads in full.
    as_of = data_push.payload_as_of(payload)
    scored = digest_engine.digest_articles(
        scored, country_display=display, iso2=iso2, as_of=as_of,
        # The text is masked already; this is about what the digest model
        # *writes*. `actors: who did what to whom` reads as an instruction to
        # name people, and people are exactly what the gazetteer cannot know.
        masked=masked,
        content_cache=digest_content_cache,
    )
    fulltext_ids = digest_engine.select_fulltext_ids(scored)
    logger.info("[%s] full-text ids: %s", iso2, fulltext_ids)

    if masked:
        # The gazetteer is a list somebody wrote; it does not know this week's
        # ministers, parties or companies. The model pass covers what it missed,
        # and only on the two or three bodies the scorer reads end to end —
        # everything else reaches it as a digest of already-masked text.
        _rewrite_fulltext(scored, fulltext_ids, iso2, cache=digest_content_cache)

    # 2c) The three-ledger evidence the model actually scores on. Separate from
    #     the panel payload above, which stays the DB-facing one: `upsert_snapshot`
    #     reads its `indicators`/`_meta.units` to write the `indicator` and
    #     `yearly_value` tables the front-end reads, and `provenance` reads its
    #     `series`. Reading every store here (rather than inside the builder)
    #     keeps the builder pure and testable without a database.
    #
    #     Each read degrades independently: a database or curated-file failure
    #     costs the country that evidence, not its score.
    evidence = data_retrieval.build_evidence_payload(
        iso2,
        as_of=as_of,
        panel=_safe(lambda: data_retrieval.query_macro_panel(iso2), iso2, "panel"),
        series=_safe(lambda: data_push.read_indicator_series(iso2), iso2, "series") or {},
        recent=_safe(lambda: data_push.read_recent_indicators(iso2), iso2, "recent") or {},
        fx_regimes=constants.FX_REGIMES,
        elections=constants.ELECTIONS,
        # Static, so it needs no vintage bound and is read the same way for a
        # 2016 anchor and for today. Degrades like every other store: a
        # malformed file costs the structural block, not the score.
        #
        # Withheld entirely for the no-structural arm, which is the one thing
        # that tells "the structural facts worked" apart from "identity never
        # mattered".
        structural={} if scoring_mode == "masked_nostructural" else
        (_safe(curated_loader.load_structural_facts, iso2, "structural") or {}),
        # Only a historical run restricts the data vintage. The daily run passes
        # None and behaves exactly as before — handing it today's date would
        # drop the current year's annual figures, whose period ends in December.
        vintage_as_of=as_of if historical else None,
    )

    # 3) LLM scoring. `as_of` is the snapshot's own date, not today's: it anchors
    #    the prompt and the sanctions lookup on the same day the row is keyed on.
    llm_output = langchain_llm.country_llm_score(
        country_display=country_name,
        payload=evidence,
        articles=scored,
        as_of=as_of,
        fulltext_ids=fulltext_ids,
        # The evidence payload names the country too, in its `_meta` and its
        # series labels, and it is serialized whole into the prompt. Masking it
        # inside the call keeps the sanctions lookup on the real code.
        mask_iso2=iso2 if masked else None,
    )

    # 3a) Lint: record contradictions between what the model flagged and what it
    #     scored. Advisory and non-blocking — nothing here changes a score, and a
    #     lint failure must not cost the country its snapshot.
    try:
        findings = lint.check(
            country_iso2=iso2,
            as_of=as_of,
            condition_flags=llm_output.get("condition_flags"),
            # lint's tripwires are on the model's 0-100 scale; the stored values
            # are already 0-1, so convert back at this one call site.
            score_3m=_to_100(llm_output.get("score_3m")),
            score_12m=_to_100(llm_output.get("score")),
            ledger_scores={
                k: _to_100(v) for k, v in (llm_output.get("ledger_scores") or {}).items()
            },
            suppressed_vol_flag=(
                evidence.get("uncertainty_inputs", {}).get("suppressed_vol_flag", {}).get("value")
            ),
            non_investable=bool(llm_output.get("non_investable")),
        )
        lint.log_findings(findings)
        data_push.upsert_lint_findings(findings)
    except Exception:
        logger.exception("[%s] lint pass failed; the snapshot still writes", iso2)

    # 3b) Provenance: hash what the model actually saw, so this row can be
    #     reproduced (or found to be irreproducible) later. Provenance is
    #     metadata, not the product — a bug in assembling it must never cost the
    #     country its score, so it degrades to NULL and the snapshot still writes.
    try:
        input_manifest = provenance.build_input_manifest(
            # `scored`, not `items`: the manifest's whole promise is that it
            # hashes the bytes the model actually saw, and under masking those
            # are not the bytes in the database.
            items=scored,
            prompt_entries=langchain_llm.prompt_entries(scored),
            fulltext_ids=fulltext_ids,
            payload=payload,
            model_id=llm_output.get("model_id"),
            prompt_version=llm_output.get("prompt_version"),
            policy_version=llm_output.get("policy_version"),
            seed=ai_client.SEED,
            masking={
                "scoring_mode": scoring_mode,
                # Without the map's version the same articles re-mask
                # differently and the row cannot be rebuilt, so this is as
                # load-bearing here as the prompt version.
                "mask_map_version": gazetteer.MASK_MAP_VERSION,
                # The hand-maintained label above says what somebody remembered
                # to write down; this says what the module actually contains.
                # The euro fix changed masking behaviour and moved neither the
                # map's data nor its version.
                "gazetteer_version": gazetteer.GAZETTEER_VERSION,
                # The gazetteer is only half of masking. The sweep rewrites what
                # the digest model wrote, and it changed twice on 2026-08-03
                # while `mask_map_version` sat still — so a row stamped with the
                # map alone cannot say which of two masking behaviours produced
                # it. Derived from the sweep prompt, so it moves whenever the
                # sweep does.
                "sweep_version": rewrite.SWEEP_VERSION,
                # "clean" by construction: `country_llm_score` raises MaskLeak
                # before sending, so any row that exists at all got past the
                # gate. Recorded anyway, because a manifest that only says what
                # went right when it went right proves nothing.
                "mask_integrity_status": "clean",
                # Five of forty-eight countries have a structural block, and
                # that asymmetry has to be countable in the data rather than
                # only in a comment.
                "structural_fields": len(evidence.get("structural") or {}),
                "identifiability": _identifiability(scored, iso2, as_of, fulltext_ids),
            } if masked else None,
        )
    except Exception:
        logger.exception("[%s] provenance manifest failed; writing the snapshot without it", iso2)
        input_manifest = None

    # 4) Rank and select Top-3 using AI's TOPIC CLUSTERING, with guaranteed length=3
    imp_map, topic_map = article_ranking.impact_topic_maps(llm_output)
    items_by_id = {it.get("id"): it for it in items if isinstance(it, dict) and it.get("id")}
    top_ids = article_ranking.select_top_ids(items_by_id, imp_map, topic_map, iso2)

    # 5) Enrich ONLY the Top-3 with missing images using the advanced scraper.
    #    Skipped for historical runs: an image is decoration, not evidence, and
    #    scraping three publishers per week per country would be thousands of
    #    live page fetches to decorate a backfill.
    if not historical:
        article_enrichment.enrich_top_images(top_ids, items_by_id)

    # 6) Build Top-3 payload AFTER enrichment
    top_articles = article_ranking.build_top_articles(top_ids, items_by_id, imp_map)

    # 6b) Add this country's Top-3 to the global alert pool (ranked after the loop)
    for a in top_articles:
        global_alert_pool.append({**a, "country_iso2": iso2, "country_name": country_name})

    # 7) Upsert to DB — unless this is a diagnostic arm, which shares
    #    (country, as_of) with its masked twin and would overwrite the
    #    production series on its own primary key. Those land in
    #    `history_run_ledger` instead, which the caller writes.
    if upsert:
        data_push.upsert_snapshot(
            {**payload, "llm_output": llm_output, "top_articles": top_articles,
             "input_manifest": input_manifest, "scoring_mode": scoring_mode},
            country_name=country_name
        )

    logger.info("[%s] score=%s (%s)", iso2, llm_output.get("score"), scoring_mode)
    logger.info("article_url: %s", [a["url"] for a in top_articles])
    logger.info("img_url: %s", [a["image"] for a in top_articles])
    return llm_output, input_manifest


def process_all_countries() -> List[Dict]:
    """Score every country in the roster and pool their Top-3 articles.

    Returns:
        The pooled Top-3 articles across all countries, each tagged with its
        originating country, ready for ``publish_global_alerts``.

    One country's failure is logged with a traceback and skipped; the rest of
    the roster still gets scored and stored.
    """
    global_alert_pool: List[Dict] = []
    for c in constants.COUNTRY_ROSTER:
        country_name, iso2 = c["name"], c["iso2"]
        try:
            _process_country(country_name, iso2, global_alert_pool)
        except Exception:
            # Resilience boundary: one country's failure must not kill the run.
            logger.exception("[%s] ERROR", iso2)
    log_run_summary()
    return global_alert_pool


def log_run_summary(as_of: Optional[date] = None) -> None:
    """What the run just wrote that somebody is supposed to look at.

    Lint is advisory by design — nothing it finds changes a score, and that was
    argued for on the basis that a contradiction gets *written down next to the
    score and read*. It was written down. `risk_lint` had no reader anywhere in
    this codebase, so the reading never happened, and an advisory tripwire
    nobody reads is indistinguishable from no tripwire.

    Degradation is the same shape one layer down. `28a8889` added
    ``digest_degraded`` and the manifest's ``stage1`` block so that "the scorer
    read digests" and "the scorer read truncated bodies" would stop being
    indistinguishable after the fact — and nothing distinguished them, so they
    stayed exactly as indistinguishable as before.

    Fully guarded: this is a summary of a run that has already written
    everything it was going to write.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    try:
        findings = data_push.read_lint_findings(as_of=as_of)
        if findings:
            by_rule: Dict[str, List[str]] = {}
            for finding in findings:
                by_rule.setdefault(finding["rule"], []).append(finding["country_iso2"])
            logger.warning("[lint] %d finding(s) on %s across %d country/ies",
                           len(findings), as_of,
                           len({f["country_iso2"] for f in findings}))
            for rule, countries in sorted(by_rule.items()):
                logger.warning("[lint]   %-34s %s", rule, ", ".join(sorted(countries)))
        else:
            logger.info("[lint] no findings on %s", as_of)
    except Exception:
        logger.exception("[lint] summary failed; the run's writes stand")

    try:
        degraded = data_push.read_stage1_degradation(as_of=as_of)
        if degraded:
            logger.warning(
                "[stage1] %d country/ies scored partly on truncated bodies "
                "rather than digests on %s:", len(degraded), as_of)
            for row in degraded:
                logger.warning("[stage1]   %s %d/%d article(s) degraded",
                               row["country_iso2"], row["degraded"], row["articles"])
        else:
            logger.info("[stage1] every article digested on %s", as_of)
    except Exception:
        logger.exception("[stage1] summary failed; the run's writes stand")


def publish_global_alerts(global_alert_pool: List[Dict]) -> None:
    """Rank the pooled Top-3 articles by importance to the global economy and
    persist the top-N to ``news_alert``.

    Runs last and is fully guarded: the per-country snapshots are already
    written by this point, so a failure here must not undo them.
    """
    try:
        ranked_alerts = alerts_ranker.rank_global_alerts(global_alert_pool)
        if ranked_alerts:
            data_push.upsert_news_alerts(
                ranked_alerts, as_of=datetime.now(timezone.utc).date()
            )
            logger.info("[alerts] ranked %d/%d pooled articles, stored %d",
                        len(ranked_alerts), len(global_alert_pool), len(ranked_alerts))
        else:
            logger.info("[alerts] no alerts ranked from %d pooled articles (skipping upsert)", len(global_alert_pool))
    except Exception:
        logger.exception("[alerts] ERROR")
