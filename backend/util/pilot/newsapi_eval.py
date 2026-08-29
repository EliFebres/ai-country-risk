"""The newsapi.ai evaluation — one country-year, measured, nothing written.

This exists to answer a purchase decision, not to harvest. The question is
whether a 5K plan at $90/mo (or a 50K upgrade) buys a corpus worth having, and
the honest way to answer it is one country-year measured properly rather than a
roster-wide harvest that commits the money first.

**Portugal 2019 is the test because Portugal is the thinnest country in the
store.** A measured PT window has "Portugal" in 0 titles, 6 ledes and 59 bodies.
If the source is good for PT it is good everywhere; if it is thin for PT the
whole premise fails on the country that needed it most.

Nothing here writes. `upsert_articles` is never called, no checkpoint is
written, and the Guardian audit is a read. That is deliberate and not just
caution: `store.upsert_articles` resolves a URL collision with
``ON CONFLICT (url) DO UPDATE``, and the `source_system` branch of that update
hands the row to whoever supplied the body. A newsapi.ai body landing on a URL
the Guardian already has would rebrand that row `newsapi_ai` — changing
`counts_by_year`, `recovery_curve`, `reports.evidence_texture` and the probe's
`nyt_share` against a baseline that assumed a fixed source mix. Measuring must
not move the thing being measured.

The two arms
------------
Archive searches bill per *searched year*, and every page is its own billed
search. That makes window width a price, and the price is not documented below
a year — so the shape of the whole 480-country-year harvest turns on a number
nobody has measured:

* **year-wide** — one query per year, paginated to the cap. Cheap per window.
  Sorted by date, so a cap that bites returns the newest N and leaves the start
  of the year unread. An annual count cannot see that; `by_month` can.
* **monthly** — twelve queries, one or two pages each. Even coverage by
  construction. Costs more searches, and whether it costs more *tokens* depends
  entirely on how a sub-year window is billed.

Run both, read `req-tokens` off both, and the comparison decides the shape and
the production cap together.
"""

import collections
import datetime
import json
import logging
import pathlib
import statistics
from typing import Any, Dict, List, Optional

from backend.data_upsert import store
from backend.news_fetching import article_enrichment, core
from backend.news_fetching.adapters import newsapi_ai
from backend.util import config

logger = logging.getLogger(__name__)

# The Guardian comparison is **queried, not quoted**. Three different numbers
# for "Guardian's PT 2019" were in circulation when this was written — 823
# articles with 757 bodies (the task brief), 959 (`GATE2_BASELINE.md`, which
# counts article-slots across 52 weekly snapshots, so an article selected by
# four snapshots counts four times), and 642 (the store, measured 2026-08-28).
# Only the last is "articles in the store for the window", which is the only one
# comparable to what newsapi.ai returns.
#
# Hardcoding any of them would have baked a 28% error into the headline
# comparison of a purchase decision, so `guardian_baseline` asks the store.

# The selector's appetite: 20 articles a snapshot, ~4.3 snapshots a month.
_ARTICLES_PER_SNAPSHOT = 20
_SNAPSHOTS_PER_MONTH = 4.3

_HISTOGRAM_EDGES = (0, 200, 400, 600, 1000, 2000, 4000, 8000, 16000, 10**9)

# Pages per *window*, per arm, sized so both arms buy a comparable corpus and
# the comparison is about distribution rather than about depth.
_PAGE_CAP = {"year": 10, "month": 1}


def _histogram(values: List[int]) -> List[Dict[str, Any]]:
    """Bucket body lengths so the shape of the distribution is visible.

    A mean hides bimodality, and bimodality is the whole question: stubs
    clustered at 200-400 with real articles at 3,000+ makes the floor obvious,
    while a continuous spread makes it a judgement call. Edges bracket
    `config.NEWSAPI_MIN_BODY_CHARS` closely on both sides so the cut can be seen
    to be in the right place, or not.
    """
    out = []
    for low, high in zip(_HISTOGRAM_EDGES, _HISTOGRAM_EDGES[1:]):
        n = sum(1 for v in values if low <= v < high)
        out.append({"from": low, "to": None if high == 10**9 else high, "n": n})
    return out


def _truncation_signals(items: List[Dict]) -> Dict[str, Any]:
    """Look for evidence that bodies arrive truncated rather than complete.

    Their documentation is JavaScript-rendered and does not state whether a body
    can come back clipped, so this asks the data instead. Three signals, none
    conclusive alone:

    * bodies ending in an ellipsis or a "read more" tail — an explicit marker;
    * lengths piling up on one exact value — a hard cap the API did not mention;
    * the share sitting just under the configured floor.

    A real marker beats any length heuristic, because a genuine 800-character
    wire item is complete evidence and a 1,200-character teaser is not, and
    length alone cannot tell those apart.
    """
    tails = ("...", "…", "[...]", "[…]", "read more", "continue reading")
    marked = [i for i in items
              if (i.get("text") or "").strip().lower().endswith(tails)]
    lengths = [len(i.get("text") or "") for i in items if i.get("text")]
    repeats = collections.Counter(lengths).most_common(3)
    share = len(marked) / len(items) if items else 0.0
    # A handful of markers is publishers syndicating teasers; a large share, or
    # a pile-up on one exact length, is the API clipping. Measured on 100 live
    # PT articles 2026-08-28: 1 marker, no repeated length, no empty body — so
    # the markers are the publishers and the floor stays the primary test.
    #
    # The distinction is the whole point. Reporting "marker found, use it" off a
    # 1% hit rate would retire the length floor in favour of an instrument that
    # catches one stub in a hundred.
    # `core.MAX_BODY_CHARS` is our own truncation, applied in `to_item` before
    # any of this runs. A pile-up there says the harvest is clipping long
    # articles, which is worth reporting and is emphatically not evidence about
    # the API.
    ours = [c for c, n in repeats if c == core.MAX_BODY_CHARS and n > 1]
    capped = [c for c, n in repeats
              if n > max(3, 0.05 * len(items)) and c != core.MAX_BODY_CHARS]
    if capped:
        verdict = (f"SYSTEMATIC: {capped[0]} chars repeats across the sample — "
                   f"the API is clipping. Use that, not the length floor.")
    elif ours:
        n = next(n for c, n in repeats if c == core.MAX_BODY_CHARS)
        verdict = (f"{n} bodies sit exactly on core.MAX_BODY_CHARS "
                   f"({core.MAX_BODY_CHARS}) — that is *our* clip, not theirs. "
                   f"The API returned longer articles than the store keeps.")
    elif share > 0.2:
        verdict = (f"SYSTEMATIC: {share:.0%} of bodies end in a truncation "
                   f"marker. Treat the marker as the test.")
    elif marked:
        verdict = (f"publisher teasers, not API truncation: {len(marked)} of "
                   f"{len(items)} ({share:.1%}) end in a marker, no length "
                   f"pile-up. The length floor stays the primary test; the "
                   f"marker is worth keeping as a second signal for stubs.")
    else:
        verdict = "no marker and no length pile-up; the floor is the only test"
    return {
        "bodies_with_truncation_marker": len(marked),
        "marker_share": round(share, 4),
        "marker_examples": [(i.get("text") or "")[-80:] for i in marked[:3]],
        "most_repeated_lengths": [{"chars": c, "n": n} for c, n in repeats if n > 1],
        "empty_bodies": sum(1 for i in items if not (i.get("text") or "")),
        "verdict": verdict,
    }


def fetch_arm(iso2: str, start: datetime.date, end: datetime.date,
              granularity: str, max_pages: Optional[int] = None) -> Dict[str, Any]:
    """Run one arm and return what it cost and what it returned.

    Spend is read from the module's own meter rather than recomputed here, so
    the number reported is the one the API billed.
    """
    name = config.country_name(iso2)
    before = newsapi_ai.spend()
    spent = [0]
    items: List[Dict] = []
    for window_start, window_end in newsapi_ai.windows(start, end, granularity):
        items.extend(newsapi_ai.window_items(name, window_start, window_end,
                                             spent, max_pages))

    after = newsapi_ai.spend()
    # Collapse across windows: a monthly arm can return the same story twice if
    # a publisher back-dates, and counting it twice would flatter the arm.
    unique = {core.dedupe_key(i): i for i in items}
    return {
        "granularity": granularity,
        "articles": list(unique.values()),
        "tokens": spent[0],
        "calls": (after["calls"] or 0) - (before["calls"] or 0),
        "tokens_are_measured": after["tokens_are_measured"],
        "searches_billed_from_header": (after["measured_calls"] or 0)
                                       - (before["measured_calls"] or 0),
    }


def by_month(items: List[Dict]) -> Dict[str, int]:
    """Articles per calendar month.

    The measurement a year-wide page cap cannot survive quietly. Date-sorted
    pagination under a cap returns the newest N, so a year that overflows comes
    back December-heavy — and a masked series built on evidence that thins
    toward January is broken in a way an annual total shows no sign of.
    """
    out: Dict[str, int] = collections.Counter()
    for item in items:
        published = item.get("published") or ""
        if len(published) >= 7:
            out[published[:7]] += 1
    return dict(sorted(out.items()))


def by_theme(items: List[Dict]) -> Dict[str, int]:
    """Articles per theme, classified the way the store would classify them.

    This is the pre-registered failure line for the one-query shape. The live
    selector reserves `_PER_THEME_FLOOR` slots per theme inside a 20-article
    snapshot precisely so a quiet ledger is not crowded out by a loud one, and a
    broad concept query makes no promise of filling them. If a theme lands under
    its floor on more than a quarter of anchors, the shape has failed — and the
    fix is a targeted top-up search for the short themes, not six queries.
    """
    out: Dict[str, int] = collections.Counter({t: 0 for t in core.THEME_QUERIES})
    for item in items:
        for theme in core.classify_themes(item.get("title"), item.get("text")):
            out[theme] += 1
    return dict(out)


def theme_floor_fill(items: List[Dict], start: datetime.date,
                     end: datetime.date) -> Dict[str, Any]:
    """The pre-registered failure line, computed rather than eyeballed.

    `core.select_with_theme_floor` reserves `_PER_THEME_FLOOR` slots per theme
    inside each snapshot, so what matters is not a theme's annual total but
    whether **the 30-day window each anchor reads** holds enough of it. A year
    with 20 `information` articles spread evenly starves every anchor equally;
    the annual number alone cannot tell you that.

    So this walks the real anchors — weekly, the cadence the pilot scores at —
    builds each one's window the way `snapshot_select` does, and counts.

    **The line: if a theme falls short on more than a quarter of anchors, the
    one-query shape has failed for that theme.** The remedy is a targeted
    top-up search for the short themes only, not a return to six queries per
    country-year.
    """
    dated = []
    for item in items:
        raw = str(item.get("published") or "")
        try:
            when = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        dated.append((when, set(core.classify_themes(item.get("title"),
                                                     item.get("text")))))

    floor = article_enrichment._PER_THEME_FLOOR
    window = datetime.timedelta(days=config.SNAPSHOT_WINDOW_DAYS)
    anchors, short = [], collections.Counter()
    anchor = start + window
    while anchor <= end:
        anchors.append(anchor)
        in_window = [themes for when, themes in dated
                     if anchor - window <= when < anchor]
        for theme in core.THEME_QUERIES:
            if sum(1 for t in in_window if theme in t) < floor:
                short[theme] += 1
        anchor += datetime.timedelta(days=7)

    n = len(anchors) or 1
    per_theme = {theme: {"anchors_short": short.get(theme, 0),
                         "share_short": round(short.get(theme, 0) / n, 3),
                         "fails": short.get(theme, 0) / n > 0.25}
                 for theme in core.THEME_QUERIES}
    failed = [t for t, v in per_theme.items() if v["fails"]]
    return {
        "anchors": n, "floor_per_theme": floor,
        "window_days": config.SNAPSHOT_WINDOW_DAYS,
        "per_theme": per_theme,
        "failed_themes": failed,
        "verdict": ("every theme fills its floor on at least three anchors in "
                    "four" if not failed else
                    f"FAILED for {', '.join(failed)} — short on more than a "
                    f"quarter of anchors. Needs a targeted top-up search, not "
                    f"six queries."),
    }


def sufficiency(items: List[Dict], start: Optional[datetime.date] = None,
                end: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Whether each month holds enough articles for the snapshots drawn from it.

    The production cap should come from this rather than from parity with the
    Guardian. The selector consumes ~86 articles a month; anything beyond that
    is bought and never read, across 480 country-years.
    """
    monthly = by_month(items)
    # Only months the request actually asked for. A handful of articles whose
    # publisher stamp falls outside the window would otherwise invent phantom
    # months holding one article each and report them as starved.
    if start and end:
        monthly = {m: n for m, n in monthly.items()
                   if start.strftime("%Y-%m") <= m <= end.strftime("%Y-%m")}
    appetite = int(_ARTICLES_PER_SNAPSHOT * _SNAPSHOTS_PER_MONTH)
    thin = {m: n for m, n in monthly.items() if n < appetite}
    return {
        "appetite_per_month": appetite,
        "months_measured": len(monthly),
        "months_below_appetite": thin,
        "median_per_month": statistics.median(monthly.values()) if monthly else 0,
        "verdict": ("every month clears the selector's appetite; the cap can fall"
                    if not thin else
                    f"{len(thin)} month(s) under {appetite} articles — the cap is "
                    f"not what is limiting those windows"),
    }


def composition(items: List[Dict], iso2: str) -> Dict[str, Any]:
    """Distinct publishers, and how many are local to the country.

    **A masking risk, not a quality one.** A Portuguese regional paper writing
    "the government" identifies the country far more sharply than the Guardian
    writing the same words, and the masking layer works on a gazetteer of names
    rather than on tone. A heavily local mix means `llm.probe` has to be re-run
    against this corpus before it can be trusted — and Portugal is already the
    probe's hardest case: a masked bundle that passed the integrity scan with
    zero flagged tokens was still named Portugal at 0.9 confidence off the Douro
    Valley and the Algarve.
    """
    publishers = collections.Counter(i.get("source") or "?" for i in items)
    local = [i for i in items
             if ((i.get("source_location") or {}).get("country") or {})
             .get("label", {}).get("eng", "") == config.country_name(iso2)]
    return {
        "distinct_publishers": len(publishers),
        "top_publishers": publishers.most_common(15),
        "articles_from_local_outlets": len(local),
        "local_share": round(len(local) / len(items), 3) if items else 0.0,
        "distinct_local_publishers": len({i.get("source") or "?" for i in local}),
    }


def overlap(items: List[Dict], iso2: str, start: datetime.date,
            end: datetime.date) -> Dict[str, Any]:
    """How much of this is already in the store, and from which source.

    Counted **before** any write, which is the only time it can be counted at
    all: the upsert collapses both copies of a story onto one row keyed by URL,
    so afterwards there is nothing left to compare. The same reason
    `gdelt.harvest_window` reports its overlap as a note rather than deriving it
    from the row count.
    """
    urls = [core.dedupe_key(i) for i in items]
    existing = store.existing_urls(urls)
    by_source = collections.Counter()
    start_ts = datetime.datetime.combine(start, datetime.time.min,
                                         tzinfo=datetime.timezone.utc)
    end_ts = datetime.datetime.combine(end, datetime.time.max,
                                       tzinfo=datetime.timezone.utc)
    for row in store.read_window(iso2, start_ts, end_ts):
        if row["url"] in existing:
            by_source[row["source_system"]] += 1
    return {
        "fetched": len(urls),
        "already_in_store": len(existing),
        "overlap_share": round(len(existing) / len(urls), 3) if urls else 0.0,
        "by_existing_source": dict(by_source),
    }


def date_integrity(items: List[Dict], start: datetime.date,
                   end: datetime.date) -> Dict[str, Any]:
    """Confirm every published_at falls inside the requested window.

    An archive index with sloppy dates would break the no-future invariant
    silently rather than loudly: `snapshot_select` bounds its window on
    `published_at`, so an article stamped a month late is read into a snapshot
    whose anchor predates it, which is exactly the leak the whole masked series
    is built to avoid.
    """
    outside, unparseable = [], []
    for item in items:
        raw = item.get("published")
        try:
            when = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            unparseable.append(raw)
            continue
        if not (start <= when.date() <= end):
            outside.append({"url": item.get("link"), "published": raw})
    return {
        "checked": len(items),
        "outside_window": len(outside),
        "examples_outside": outside[:5],
        "unparseable": len(unparseable),
        "spot_check": [{"url": i.get("link"), "published": i.get("published")}
                       for i in items[:5]],
        "verdict": "clean" if not outside and not unparseable else "SLOPPY DATES",
    }


def body_quality(items: List[Dict]) -> Dict[str, Any]:
    """The body-length distribution, against the Guardian's average."""
    lengths = [len(i.get("text") or "") for i in items]
    with_body = [n for n in lengths if n]
    full = [n for n in lengths if n >= config.NEWSAPI_MIN_BODY_CHARS]
    return {
        "articles": len(items),
        "with_any_text": len(with_body),
        "at_or_above_floor": len(full),
        "below_floor_but_present": len(with_body) - len(full),
        "no_text_at_all": len(lengths) - len(with_body),
        "floor": config.NEWSAPI_MIN_BODY_CHARS,
        "mean_chars_of_full": round(statistics.mean(full)) if full else 0,
        "median_chars_of_full": round(statistics.median(full)) if full else 0,
        "floor_note": "guardian applies no floor at all; see guardian_stub_audit",
        "histogram": _histogram(lengths),
        "truncation": _truncation_signals(items),
    }


# ---------------------------------------------------------------------------
# The Guardian audit — independent of newsapi.ai, and the more urgent question
# ---------------------------------------------------------------------------

def guardian_baseline(iso2: str, start: datetime.date,
                      end: datetime.date) -> Dict[str, Any]:
    """What the store already holds for this window, per source.

    The yardstick the evaluation measures against, read at run time rather than
    remembered — see the note above on the three incompatible numbers that were
    all called "Guardian's PT 2019".
    """
    rows = store._rows("""
        SELECT source_system,
               COUNT(*)::int                                     AS articles,
               COUNT(*) FILTER (WHERE body IS NOT NULL)::int      AS with_body,
               ROUND(AVG(length(body)))::int                      AS mean_chars
          FROM article
         WHERE country_iso2 = %s AND published_at >= %s AND published_at < %s
         GROUP BY 1 ORDER BY 1
    """, (iso2, start, end + datetime.timedelta(days=1)))
    return {row["source_system"]: row for row in rows}


def guardian_stub_audit(min_chars: Optional[int] = None) -> Dict[str, Any]:
    """How many Guardian rows called ``recovered`` are actually stubs.

    `adapters.guardian` applies no length test whatsoever — `if
    item.get("text")` — so a one-character body is stored there as `recovered`,
    and nothing downstream re-checks it. That means the corpus's headline
    body-coverage number has never been validated against what a body actually
    is.

    This matters more than the newsapi.ai question it came from. Gate 2's
    evidence quality was measured on those rows, and the p3 context blocks were
    built on them; if a meaningful share are stubs, both were overstated. It is
    a read and a cheap one.

    **Changes nothing.** No Guardian row is rewritten by this function or by
    anything this module does.
    """
    floor = min_chars if min_chars is not None else config.NEWSAPI_MIN_BODY_CHARS
    rows = store._rows("""
        SELECT source_system,
               COUNT(*)::int                                        AS recovered_rows,
               COUNT(*) FILTER (WHERE COALESCE(length(body), 0) < %s)::int AS below_floor,
               COUNT(*) FILTER (WHERE COALESCE(length(body), 0) < 400)::int AS stub_400,
               COUNT(*) FILTER (WHERE body IS NULL)::int            AS null_body,
               ROUND(AVG(length(body)))::int                        AS mean_chars,
               MIN(length(body))::int                               AS min_chars,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY length(body))::int AS median_chars
          FROM article
         WHERE body_status = 'recovered'
         GROUP BY 1 ORDER BY 1
    """, (floor,))
    return {"floor": floor, "by_source": rows}


# ---------------------------------------------------------------------------
# Fixtures, so the write path is testable without spending again
# ---------------------------------------------------------------------------

def save_fixtures(items: List[Dict], where: pathlib.Path, limit: int = 12) -> pathlib.Path:
    """Save a handful of real articles for the DB-gated tests to run against.

    Real payloads rather than hand-written ones, because the shapes that break a
    parser are the ones nobody thinks to invent: a missing body, a source with
    no location, a date with no offset. Bodies are clipped — the fixtures are
    for exercising the write path, not for storing someone's journalism in the
    repo — and no API key is anywhere near them, since these are normalized
    items rather than raw requests.
    """
    where.parent.mkdir(parents=True, exist_ok=True)
    # The clip is what keeps someone's journalism out of the repo, and it is
    # also what would destroy the evidence truncation detection runs on: a body
    # cut at 1,500 characters ends mid-sentence and looks truncated whatever the
    # API did. So the true length and the true ending are recorded beside the
    # clipped text rather than being clipped away with it.
    sample = []
    for i in items[:limit]:
        text = i.get("text") or ""
        sample.append({**i, "text": text[:1500],
                       "_body_chars": len(text), "_body_tail": text[-160:]})
    where.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
    return where


def evaluate(iso2: str = "PT", year: int = 2019, arms: tuple = ("year", "month"),
             fixtures: Optional[pathlib.Path] = None,
             start: Optional[datetime.date] = None,
             end: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Run the evaluation and return every measurement. Writes nothing.

    Args:
        iso2: country to evaluate. PT by default — the thinnest in the store.
        year: the country-year, unless ``start``/``end`` override it.
        arms: which window granularities to run. Both, by default: the
            comparison is the point.
        fixtures: where to save sample articles, or None to skip.
        start: explicit window start, overriding ``year``. Exists because an
            account without archive entitlement can still be measured over the
            window it *can* reach — which answers body quality, source mix and
            theme fill even when it cannot answer anything about 2019.
        end: explicit window end, overriding ``year``.

    Returns:
        One dict per arm plus the projection and the Guardian audit.
    """
    start = start or datetime.date(year, 1, 1)
    end = end or datetime.date(year, 12, 31)
    baseline = guardian_baseline(iso2, start, end)
    out: Dict[str, Any] = {"country": iso2, "year": year, "arms": {},
                           "start": start.isoformat(), "end": end.isoformat(),
                           "baseline": baseline}
    # An evaluation that silently reports a substitute window under the label of
    # the one that was asked for is worse than one that fails outright.
    if (start, end) != (datetime.date(year, 1, 1), datetime.date(year, 12, 31)):
        out["proxy_note"] = (
            f"PROXY WINDOW. This is {start}..{end}, NOT the {year} country-year. "
            f"Volume, monthly distribution and token cost do NOT transfer to an "
            f"archive year; body quality, source mix and theme fill do.")

    for granularity in arms:
        logger.info("[eval] %s %s — %s-wide windows", iso2, year, granularity)
        # Per-window caps, because the token price is per window. Ten pages
        # over a year and one page a month both buy ~1,000 articles; the
        # difference is 50 tokens against 60, and whether the year's evidence
        # is evenly spread or piled at whichever end the sort favours.
        cap = _PAGE_CAP.get(granularity)
        try:
            arm = fetch_arm(iso2, start, end, granularity, cap)
        except newsapi_ai.TokenBudgetExhausted as exc:
            # Report what the arm bought before the cap bit rather than losing
            # the whole run to it. The budget is the point of the exercise.
            out["arms"][granularity] = {"stopped_on_budget": str(exc)}
            continue
        items = arm.pop("articles")
        out["arms"][granularity] = {
            **arm,
            "volume": {"articles": len(items), "already_in_store": baseline},
            "body_quality": body_quality(items),
            "by_month": by_month(items),
            "by_theme": by_theme(items),
            "sufficiency": sufficiency(items, start, end),
            "theme_floor": theme_floor_fill(items, start, end),
            "composition": composition(items, iso2),
            "overlap": overlap(items, iso2, start, end),
            "date_integrity": date_integrity(items, start, end),
        }
        if fixtures and granularity == arms[0]:
            out["fixtures"] = str(save_fixtures(items, fixtures))

    out["projection"] = projection(out["arms"])
    out["guardian_audit"] = guardian_stub_audit()
    return out


def projection(arms: Dict[str, Any]) -> Dict[str, Any]:
    """Token cost for the two roster sizes, from the arm that actually ran.

    Projected from measured spend rather than from the price list, for the
    reason `score.projection` raises rather than returning a constant: a
    projection with nothing to project from is a refusal, and a number returned
    as a float is indistinguishable from a measurement to whoever approves the
    spend.
    """
    out = {}
    for granularity, arm in arms.items():
        per_country_year = arm["tokens"]
        out[granularity] = {
            "tokens_per_country_year": per_country_year,
            "measured": arm["tokens_are_measured"],
            "roster_5x10": per_country_year * 50,
            "roster_48x10": per_country_year * 480,
            "months_of_5k_plan_for_48x10": round(per_country_year * 480 / 5000, 1),
            "overage_usd_for_48x10_on_5k_plan":
                round(max(0, per_country_year * 480 - 5000) * 0.015, 2),
        }
    return out


def render(result: Dict[str, Any]) -> None:
    """Print the evaluation as something a purchase decision can be made from.

    Deliberately not a JSON dump. The numbers that decide this are the token
    cost, the body-length shape and the monthly distribution, and those have to
    be readable side by side or the comparison does not get made.
    """
    iso2 = result["country"]
    window = f"{result['start']}..{result['end']}"
    print()
    print("=" * 72)
    print(f"newsapi.ai evaluation — {iso2} {window} — nothing written")
    if result.get("proxy_note"):
        print(f"!! {result['proxy_note']}")
    print("=" * 72)

    for granularity, arm in result["arms"].items():
        vol, bq = arm["volume"], arm["body_quality"]
        measured = "measured from req-tokens" if arm["tokens_are_measured"] \
            else "ASSERTED from the price list — no billing header seen"
        print(f"\n--- {granularity}-wide windows ---")
        print(f"  tokens      {arm['tokens']:>6}  ({measured}), {arm['calls']} search(es)")
        already = "  ".join(f"{k}={v['articles']}"
                            for k, v in (vol["already_in_store"] or {}).items())
        print(f"  articles    {vol['articles']:>6}  (already in store: {already or 'none'})")
        print(f"  bodies      {bq['at_or_above_floor']:>6} at/above the "
              f"{bq['floor']}-char floor; {bq['below_floor_but_present']} short, "
              f"{bq['no_text_at_all']} none")
        guardian_mean = (result.get("baseline", {}).get("guardian") or {}).get("mean_chars")
        print(f"              mean {bq['mean_chars_of_full']} chars, median "
              f"{bq['median_chars_of_full']}  (Guardian same window: "
              f"{guardian_mean})")
        print("  length histogram:")
        for bucket in bq["histogram"]:
            if bucket["n"]:
                top = bucket["to"] if bucket["to"] else "+"
                print(f"    {bucket['from']:>6}-{str(top):<6} {'#' * min(60, bucket['n'])} {bucket['n']}")
        print(f"  truncation  {bq['truncation']['verdict']}")
        if bq["truncation"]["most_repeated_lengths"]:
            print(f"              repeated lengths: {bq['truncation']['most_repeated_lengths']}")

        print("  by month:   " + "  ".join(f"{m}={n}" for m, n in arm["by_month"].items()))
        print("  by theme:   " + "  ".join(f"{t}={n}" for t, n in arm["by_theme"].items()))
        print(f"  sufficiency {arm['sufficiency']['verdict']}")
        tf = arm["theme_floor"]
        print(f"  theme floor over {tf['anchors']} weekly anchors, "
              f"{tf['floor_per_theme']} per theme in a "
              f"{tf['window_days']}-day window:")
        print("              " + "  ".join(
            f"{t}={v['share_short']:.0%}short" for t, v in tf["per_theme"].items()))
        print(f"              {tf['verdict']}")
        comp = arm["composition"]
        print(f"  publishers  {comp['distinct_publishers']} distinct; "
              f"{comp['articles_from_local_outlets']} articles from "
              f"{comp['distinct_local_publishers']} local outlet(s) "
              f"(local share {comp['local_share']})")
        print(f"              top: {', '.join(n for n, _ in comp['top_publishers'][:6])}")
        ov = arm["overlap"]
        print(f"  overlap     {ov['already_in_store']} of {ov['fetched']} already "
              f"in the store ({ov['overlap_share']}) — {ov['by_existing_source']}")
        print(f"  dates       {arm['date_integrity']['verdict']}: "
              f"{arm['date_integrity']['outside_window']} outside the window, "
              f"{arm['date_integrity']['unparseable']} unparseable")

    print(f"\n--- projection, from measured spend ---")
    for granularity, proj in result["projection"].items():
        flag = "" if proj["measured"] else "  [ASSERTED, not measured]"
        print(f"  {granularity:<6} {proj['tokens_per_country_year']:>5} tok/country-year"
              f" -> 5x10 {proj['roster_5x10']:>6}"
              f" | 48x10 {proj['roster_48x10']:>7}"
              f" ({proj['months_of_5k_plan_for_48x10']} months of the 5K plan,"
              f" ${proj['overage_usd_for_48x10_on_5k_plan']} overage){flag}")

    print(f"\n--- Guardian stub audit (read-only; no Guardian row changed) ---")
    audit = result["guardian_audit"]
    print(f"  floor {audit['floor']} chars, over rows already marked 'recovered':")
    for row in audit["by_source"]:
        print(f"    {row['source_system']:<12} {row['recovered_rows']:>7} recovered  "
              f"{row['below_floor']:>6} below floor  {row['stub_400']:>6} under 400  "
              f"{row['null_body']:>5} null  "
              f"mean {row['mean_chars']} / median {row['median_chars']} / min {row['min_chars']}")
    print()
