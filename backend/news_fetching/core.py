"""
Retrieval: ask for the material, not the country.

A single query for a country's name returns what a general newsroom publishes
about that country, which for most countries in most weeks is sport, travel and
culture. Asking six questions that match the ledgers the score is built from
returns the material the score is actually about. The queries are the instrument;
everything downstream can only rank what these bring back.

Three decisions live here, each of which was previously somewhere it should not
have been:

**The query names.** The roster carries World Bank labels, and the pipeline used
them as exact quoted phrases. `"Hong Kong SAR, China"` is a phrase no headline
contains. `QUERY_NAME_OVERRIDES` is measured, not guessed: `roster_name_probe`
fetches each roster name against its colloquial form and only the names that
actually move recall are listed.

**Depth.** Ten per theme. Sixty candidates a country is enough for a 20-article
budget after a relevance gate rejects most of them; the cost of a deeper sweep
falls on the classifier, which reads every candidate.

**Dedupe.** On the resolved publisher link, not the Google News wrapper URL. The
same story arrives under several wrappers, and deduping on the wrapper lets the
same article occupy three of twenty slots. `fetch_links` already resolves every
item it keeps, so this costs nothing extra.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from backend.news_fetching import fetch_links

__all__ = [
    "THEMES",
    "THEME_QUERIES",
    "DEPTH",
    "WINDOW_DAYS",
    "QUERY_NAME_OVERRIDES",
    "COLLOQUIAL_CANDIDATES",
    "query_name",
    "build_queries",
    "headline_key",
    "dedupe",
    "fetch_candidates",
]


# --- The six questions ------------------------------------------------------
#
# Five match the ledgers the score is built from, so a ledger cannot go unasked;
# `broad` is the catch-all that picks up whatever the five miss. `security` has
# no ledger of its own — conflict enters the score through `order` — but it needs
# its own query, because security reporting does not use the vocabulary of the
# other five and a combined query loses it.

THEMES = ("friction", "order", "security", "information", "edge", "broad")

THEME_QUERIES: Dict[str, str] = {
    "friction": '"{c}" (tax OR taxation OR fiscal OR inflation OR currency OR '
                'corruption OR regulation OR tariff OR subsidy)',
    "order": '"{c}" (government OR election OR parliament OR cabinet OR coup OR '
             'protest OR "rule of law" OR court OR judiciary OR "state of emergency")',
    "security": '"{c}" (conflict OR military OR sanctions OR terrorism OR '
                'insurgency OR border OR unrest)',
    "information": '"{c}" ("press freedom" OR journalist OR censorship OR '
                   'transparency OR "statistics office" OR audit OR "digital government")',
    "edge": '"{c}" ("business formation" OR investment OR startup OR education OR '
            'university OR "skilled migration" OR research)',
    "broad": '"{c}" (economy OR politics)',
}

# Ten per theme. Measuring depth 100 was reported to add almost nothing on the
# starved themes while multiplying classification cost fivefold — that figure is
# not reproducible on this branch and is treated as a claim, but ten is the
# depth the budget and the gate are sized for either way.
DEPTH = 10

WINDOW_DAYS = 30


# --- Query names ------------------------------------------------------------
#
# Candidates to measure, not conclusions. `roster_name_probe` fetches both forms
# for each of these and prints the recall of each; only the names that move go
# into QUERY_NAME_OVERRIDES below. A country whose roster name is already what
# the press writes is absent from both maps.

COLLOQUIAL_CANDIDATES: Dict[str, str] = {
    "HK": "Hong Kong",
    "AE": "UAE",
    "GB": "Britain",
    "US": "America",
    "KR": "South Korea",
    "TR": "Turkey",
    "CZ": "Czech Republic",
    "NL": "Netherlands",
    "RU": "Russia",
}

# Measured overrides, from `roster_name_probe` on 2026-09-22 (10 per theme,
# 30-day window, totals across all six). A name is here only because its
# colloquial form retrieved materially more.
#
#   HK  'Hong Kong SAR, China'   1  vs  'Hong Kong'       60   x60.00
#   AE  'United Arab Emirates'  41  vs  'UAE'             59   x1.44
#   CZ  'Czechia'               31  vs  'Czech Republic'  44   x1.42
#
# Hong Kong is the case that justifies the whole exercise: one article across six
# themes, because the World Bank's label is a phrase no headline contains and the
# query quotes it exactly. Five of its six themes returned nothing at all.
#
# Measured and NOT overridden, because the gain was inside the noise band:
#   GB  'United Kingdom'  47  vs  'Britain'   54  x1.15
#   US  'United States'   53  vs  'America'   58  x1.09
#
# Both nonetheless move the `information` theme specifically — GB 0 -> 4, US
# 3 -> 8 — which is the thinnest ledger and the one a starved query hurts most.
# Overriding them is a precision risk the totals do not capture: "America" also
# matches Latin, South and Central America. Left as measured; the relevance gate
# is what can settle it, so it is recorded in docs/deferred.md for the
# verification session rather than decided here on recall alone.
QUERY_NAME_OVERRIDES: Dict[str, str] = {
    "HK": "Hong Kong",
    "AE": "UAE",
    "CZ": "Czech Republic",
}


def query_name(iso2: str, roster_name: str) -> str:
    """Return the name to put in a query for this country.

    Args:
        iso2: The roster's ISO-2 code.
        roster_name: The roster's display name.

    Returns:
        The measured override where one exists, otherwise the roster name.
    """
    return QUERY_NAME_OVERRIDES.get(iso2, roster_name)


def build_queries(name: str) -> Dict[str, str]:
    """Return ``{theme: query}`` for one country name."""
    return {theme: tpl.format(c=name) for theme, tpl in THEME_QUERIES.items()}


# --- Dedupe -----------------------------------------------------------------

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACE = re.compile(r"\s+")


def headline_key(title: str, publisher: str = "") -> str:
    """Return a cheap identity key for a story.

    Wire copy runs under the same headline at a dozen outlets, and the same
    outlet re-lists its own story under several wrapper URLs. This catches the
    second case before the publisher link is even consulted; the publisher link
    catches the rest.

    The publisher is deliberately *not* part of the key. Two outlets running the
    identical AP headline are one story for scoring purposes, and letting both in
    spends two of twenty slots on one fact.
    """
    t = _PUNCT.sub(" ", (title or "").lower())
    # Google News suffixes the outlet onto the title: "Headline - Reuters".
    if publisher:
        p = _PUNCT.sub(" ", publisher.lower()).strip()
        if p and t.strip().endswith(p):
            t = t.strip()[: -len(p)]
    return _SPACE.sub(" ", t).strip()


def dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse duplicate stories, merging the themes that found each one.

    Order is preserved, first occurrence wins, and the surviving item's
    ``themes`` list accumulates every theme whose query returned it — a story
    about a central bank under political pressure is genuinely both `friction`
    and `order`, and throwing away the second finding loses that.
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for item in items:
        link = (item.get("publisher_link") or item.get("link") or "").strip()
        key = link or headline_key(item.get("title", ""), item.get("source", ""))
        hkey = headline_key(item.get("title", ""), item.get("source", ""))
        seen = by_key.get(key) or (by_key.get(hkey) if hkey else None)
        if seen is not None:
            for theme in item.get("themes", []):
                if theme not in seen["themes"]:
                    seen["themes"].append(theme)
            continue
        kept = dict(item)
        kept["themes"] = list(item.get("themes", []))
        by_key[key] = kept
        if hkey:
            by_key.setdefault(hkey, kept)
        out.append(kept)
    return out


# --- The pool ---------------------------------------------------------------


def fetch_candidates(
    country_name: str,
    iso2: str,
    *,
    depth: int = DEPTH,
    window_days: int = WINDOW_DAYS,
    extract_chars: int = 24000,
) -> Dict[str, Any]:
    """Fetch and deduplicate one country's candidate pool.

    This is the pool everything else draws from. It does no ranking and no
    filtering on relevance — that is the gate's job, and mixing the two is how a
    keyword heuristic ended up deciding what the model was allowed to read.

    Args:
        country_name: The roster display name.
        iso2: The roster ISO-2 code, used to look up a measured query name.
        depth: Results requested per theme.
        window_days: Age window, applied to the feed date and then again to the
            article's own date.
        extract_chars: Body extraction cap passed to `fetch_links`.

    Returns:
        ``{"items": [...], "report": {...}}``. The report carries per-theme
        candidate counts, the count after dedupe, the stale-republication count
        and the top publishers — the per-country pool report.
    """
    name = query_name(iso2, country_name)
    queries = build_queries(name)

    collected: List[Dict[str, Any]] = []
    per_theme: Dict[str, int] = {}
    errors: Dict[str, str] = {}

    for theme, query in queries.items():
        try:
            items = fetch_links.gnews_rss(
                query,
                max_results=depth,
                expand=True,
                extract_chars=extract_chars,
                max_age_days=window_days,
            )
        except Exception as e:  # one theme failing must not cost the other five
            per_theme[theme] = 0
            errors[theme] = str(e)
            continue
        for item in items:
            item["themes"] = [theme]
        per_theme[theme] = len(items)
        collected.extend(items)

    stale = [i for i in collected if i.get("stale_republication")]
    fresh = [i for i in collected if not i.get("stale_republication")]
    deduped = dedupe(fresh)

    publishers: Dict[str, int] = {}
    for item in deduped:
        pub = (item.get("source") or "").strip() or "(unknown)"
        publishers[pub] = publishers.get(pub, 0) + 1

    return {
        "items": deduped,
        "report": {
            "country_iso2": iso2,
            "query_name": name,
            "query_name_overridden": name != country_name,
            "per_theme": per_theme,
            "fetched": len(collected),
            "stale_republications": len(stale),
            "after_dedupe": len(deduped),
            "top_publishers": sorted(
                publishers.items(), key=lambda kv: (-kv[1], kv[0])
            )[:10],
            "errors": errors,
        },
    }


def format_pool_report(report: Dict[str, Any]) -> str:
    """Render one country's pool report as a line for the run output."""
    themes = " ".join(f"{t}={report['per_theme'].get(t, 0)}" for t in THEMES)
    top = ", ".join(f"{p}({n})" for p, n in report["top_publishers"][:5])
    name_note = f" as {report['query_name']!r}" if report["query_name_overridden"] else ""
    return (
        f"[pool] {report['country_iso2']}{name_note}: {themes} | "
        f"fetched={report['fetched']} stale={report['stale_republications']} "
        f"deduped={report['after_dedupe']} | top: {top}"
    )
