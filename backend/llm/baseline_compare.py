"""
What the old selection would have chosen, reconstructed from the stored pool.

Part 7 of the payload brief asks what the gate is being measured *against*. The
honest answer needs the twenty articles the pre-gate pipeline would have picked,
listed by title and publisher, so the before and after can be read side by side
rather than compared as percentages.

**This is a reconstruction, not a replay.** `_score_article_relevance` was
deleted when the gate replaced it. It is recovered here verbatim from this
branch's own history — `git show 8fad0bf^:backend/util/pipeline.py` — and run
against the candidate pool the current retrieval stored. Two consequences worth
stating plainly wherever these numbers are quoted:

* The pool is the **new** one: six theme queries, ten deep, deduped on the
  publisher link. The old pipeline fetched four queries fifteen deep and deduped
  on the Google wrapper URL, so it saw a different and generally worse pool.
  What this shows is the old *selection rule* on today's evidence, which
  isolates the rule — the thing the gate replaced — from the retrieval changes
  that happened at the same time.
* `summary` is reconstructed from the stored body. The old scorer read
  `summary or snippet`, where `summary` was the first 240 words of the extracted
  text; the stored `article.body` holds that text, so the first 240 words of it
  are the same string the old scorer saw.

    python -m backend.llm.baseline_compare --countries US,PT,KW
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional

from backend.data_upsert import store
from backend.util import constants

__all__ = ["score_relevance_old", "old_selection", "report"]


# The budget and threshold the old pipeline used.
OLD_THRESHOLD = 0.3
OLD_BUDGET = 20
OLD_SUMMARY_WORDS = 240


# --- recovered verbatim from 8fad0bf^:backend/util/pipeline.py --------------
#
# Do not tidy this. It is evidence, and its defects are the point: the 0.1 floor
# whenever the roster's formal name is not a substring of title+summary, the
# base score that equals the threshold, and a sport penalty that is a substring
# match.

_HIGH_KEYWORDS = [
    'government', 'ministry', 'parliament', 'president', 'prime minister',
    'central bank', 'interest rate', 'monetary policy', 'inflation', 'gdp',
    'election', 'cabinet', 'policy', 'budget', 'fiscal', 'trade',
    'military', 'defense', 'conflict', 'sanctions', 'war', 'coup', 'security'
]

_MEDIUM_KEYWORDS = [
    'economy', 'economic', 'finance', 'currency', 'debt', 'growth',
    'minister', 'official', 'regulation', 'law', 'reform'
]

_NOISE_KEYWORDS = [
    'sport', 'football', 'soccer', 'basketball', 'tennis', 'cricket',
    'music', 'entertainment', 'celebrity', 'festival', 'award',
    'movie', 'film', 'actor', 'singer', 'concert'
]


def score_relevance_old(article: Dict[str, Any], country_name: str) -> float:
    """The deleted keyword heuristic, unchanged."""
    title = (article.get("title") or "").lower()
    summary = (article.get("summary") or article.get("snippet") or "").lower()
    text = f"{title} {summary}"
    country_lower = country_name.lower()

    if country_lower not in text:
        return 0.1

    score = 0.3

    high_count = sum(1 for kw in _HIGH_KEYWORDS if kw in text)
    medium_count = sum(1 for kw in _MEDIUM_KEYWORDS if kw in text)
    noise_count = sum(1 for kw in _NOISE_KEYWORDS if kw in text)

    score += min(high_count * 0.15, 0.5)
    score += min(medium_count * 0.08, 0.2)
    score -= noise_count * 0.2

    if any(kw in title for kw in _HIGH_KEYWORDS):
        score += 0.15

    return max(0.0, min(1.0, score))


def _as_old_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a stored article the way the old scorer expected to receive it."""
    body = row.get("body") or ""
    summary = " ".join(body.split()[:OLD_SUMMARY_WORDS])
    return {
        "title": row.get("title") or "",
        "summary": summary,
        "snippet": row.get("abstract") or "",
        "published": (row.get("page_published_at") or row.get("published_at")),
        "source": row.get("publisher") or "",
        "url": row.get("url"),
    }


def old_selection(rows: List[Dict[str, Any]], country_name: str) -> Dict[str, Any]:
    """Apply the old rule to a stored candidate pool.

    The rule, as it stood: score everything, keep `>= 0.3`, sort by relevance
    then recency, take twenty. If fewer than three clear the bar, throw the bar
    away and take the top twenty of everything — the top-up that put whatever
    was left at the floor into the payload.
    """
    scored = []
    for row in rows:
        item = _as_old_item(row)
        item["relevance_score"] = score_relevance_old(item, country_name)
        scored.append(item)

    def sort_key(it):
        return (it.get("relevance_score", 0.0), str(it.get("published") or ""))

    cleared = [i for i in scored if i["relevance_score"] >= OLD_THRESHOLD]
    cleared.sort(key=sort_key, reverse=True)

    topped_up = False
    if len(cleared) < 3:
        topped_up = True
        cleared = sorted(scored, key=sort_key, reverse=True)

    selected = cleared[:OLD_BUDGET]
    at_floor = sum(1 for i in selected if i["relevance_score"] <= 0.1)

    return {
        "selected": selected,
        "pool": len(scored),
        "cleared_bar": sum(1 for i in scored if i["relevance_score"] >= OLD_THRESHOLD),
        "topped_up_below_the_bar": topped_up,
        "selected_at_the_floor": at_floor,
    }


def report(countries: List[str]) -> Dict[str, Any]:
    """Print the old selection for each country, titles and publishers."""
    names = constants.COUNTRY_NAME_BY_ISO2
    out: Dict[str, Any] = {}

    for iso2 in countries:
        name = names.get(iso2)
        if not name:
            print(f"{iso2}: not in the roster, skipped")
            continue

        rows = store.read_articles(iso2)
        if not rows:
            print(f"{iso2}: no stored candidates — run the ETL for it first")
            continue

        got = old_selection(rows, name)
        out[iso2] = got

        print("")
        print(f"=== {iso2} ({name}) — the old selection, reconstructed ===")
        print(f"pool {got['pool']} | cleared the 0.3 bar {got['cleared_bar']} | "
              f"topped up below the bar: {got['topped_up_below_the_bar']} | "
              f"selected at the 0.1 floor: {got['selected_at_the_floor']}")
        print("")
        for i, item in enumerate(got["selected"], start=1):
            pub = (item.get("source") or "(unknown)")[:30]
            print(f"{i:>3} {item['relevance_score']:.3f}  {pub:<30}  {item['title'][:95]}")

    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--countries", default="US,PT,KW")
    args = ap.parse_args()
    report([c.strip().upper() for c in args.countries.split(",") if c.strip()])


if __name__ == "__main__":
    main()
