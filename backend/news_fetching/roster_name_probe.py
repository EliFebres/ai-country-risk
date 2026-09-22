"""
Measure each roster name against its colloquial form.

The roster carries World Bank labels because that is what the World Bank API
wants. Google News wants what a headline writer would type. `"Hong Kong SAR,
China"` is a phrase no headline contains, and because the query quotes the name
exactly, Google will not loosen it — the result is a country that retrieves a
fraction of what it should, quietly, forever.

This probe exists so `core.QUERY_NAME_OVERRIDES` is measured rather than argued
about. It runs the six theme queries under both forms and prints the counts. A
name earns an override by retrieving materially more; a name that does not move
stays as the roster has it.

Only the names in `core.COLLOQUIAL_CANDIDATES` are probed. For the other 39
roster entries the display name already *is* the colloquial form — "Brazil",
"France", "Japan" — so there is no second form to measure and nothing to decide.

Free to run: RSS only, no body extraction, no model call.

    python -m backend.news_fetching.roster_name_probe
"""

from __future__ import annotations

import sys
from typing import Dict, List, Tuple

from backend.news_fetching import core, fetch_links
from backend.util import constants

# A name has to beat its roster form by this much to be worth an override.
# Retrieval counts wobble run to run; a couple of extra articles is noise, and
# an override that is noise is a permanent unexplained difference between two
# countries' evidence.
MATERIAL_GAIN = 1.25


def _count(name: str, *, depth: int = core.DEPTH) -> Tuple[int, Dict[str, int]]:
    """Return (total, per-theme counts) for one name, without expanding bodies."""
    per_theme: Dict[str, int] = {}
    for theme, query in core.build_queries(name).items():
        try:
            items = fetch_links.gnews_rss(
                query,
                max_results=depth,
                expand=False,            # counts only — no body fetch, no cost
                build_summary=False,
                max_age_days=core.WINDOW_DAYS,
            )
            per_theme[theme] = len(items)
        except Exception as e:
            print(f"    {theme}: ERROR {e}", file=sys.stderr)
            per_theme[theme] = 0
    return sum(per_theme.values()), per_theme


def run() -> List[Dict[str, object]]:
    """Probe every candidate and print the table. Returns the rows."""
    names = constants.COUNTRY_NAME_BY_ISO2
    rows: List[Dict[str, object]] = []

    print(f"Probing {len(core.COLLOQUIAL_CANDIDATES)} candidate names "
          f"({core.DEPTH} per theme, {core.WINDOW_DAYS}-day window)\n")

    for iso2, colloquial in core.COLLOQUIAL_CANDIDATES.items():
        roster_name = names.get(iso2)
        if roster_name is None:
            print(f"{iso2}: not in the roster, skipped")
            continue
        if roster_name == colloquial:
            print(f"{iso2}: roster name is already the colloquial form, skipped")
            continue

        roster_total, roster_themes = _count(roster_name)
        colloq_total, colloq_themes = _count(colloquial)
        gain = (colloq_total / roster_total) if roster_total else float("inf")
        verdict = "OVERRIDE" if gain >= MATERIAL_GAIN else "keep roster name"

        rows.append({
            "iso2": iso2,
            "roster_name": roster_name,
            "colloquial": colloquial,
            "roster_total": roster_total,
            "colloquial_total": colloq_total,
            "gain": gain,
            "verdict": verdict,
            "roster_themes": roster_themes,
            "colloquial_themes": colloq_themes,
        })

        print(
            f"{iso2}  {roster_name!r} {roster_total:>3}  vs  "
            f"{colloquial!r} {colloq_total:>3}   "
            f"x{gain:.2f}  -> {verdict}"
        )
        print(f"      roster:     {roster_themes}")
        print(f"      colloquial: {colloq_themes}")

    print("\nQUERY_NAME_OVERRIDES = {")
    for r in rows:
        if r["verdict"] == "OVERRIDE":
            print(f'    "{r["iso2"]}": "{r["colloquial"]}",'
                  f'   # {r["roster_total"]} -> {r["colloquial_total"]}')
    print("}")

    return rows


if __name__ == "__main__":
    run()
