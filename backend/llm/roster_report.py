"""
The roster-wide picture: what the gate did across all 48, and what came back.

Reads the census and the snapshots a full run left behind, and writes the tables
Part 7.5 and Part 9 ask for. Takes no model calls of its own except when asked
to recover a gate reason that was not stored — see `reason_for`.

    python -m backend.llm.roster_report
"""

from __future__ import annotations

import argparse
import datetime as dt
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence

import psycopg2

from backend.data_upsert import store
from backend.data_upsert.data_push import DB_URL
from backend.llm import constants as ai_constants
from backend.llm import relevance
from backend.util import constants, paths

__all__ = ["load_run", "country_table", "rejected_publishers",
           "noise_that_got_through", "round_number_shares", "reason_for"]


OUT = paths.PROJECT_ROOT / "docs" / "roster-run.md"

# Words that should never describe an article the gate let through. Deliberately
# blunt: this is a net for obvious failures, not a classifier. A hit is a
# candidate for inspection, not a verdict — "Ajax" is a football club and a
# Dutch town, and "strike" is industrial action far more often than it is sport.
NOISE_WORDS = (
    "football", "soccer", "basketball", "cricket", "tennis", "olympic",
    "world cup", "premier league", "champions league", "nba", "nfl", "fifa",
    "celebrity", "royal", "prince ", "princess", "actor", "actress", "singer",
    "album", "concert", "box office", "netflix", "movie", "film star",
    "horoscope", "recipe", "obituary", "wedding",
)


def _connect():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DB_URL)


def load_run(run_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """Read every census row and snapshot for one run date."""
    run_date = run_date or dt.date.today()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT country_iso2, evidence_coverage, payload_fingerprint,
                       indicators, articles, versions
                  FROM payload_census WHERE as_of = %s ORDER BY country_iso2
                """,
                (run_date,),
            )
            census = {
                r[0]: {"evidence_coverage": r[1], "payload_fingerprint": r[2],
                       "indicators": r[3], "articles": r[4], "versions": r[5]}
                for r in cur.fetchall()
            }
            cur.execute(
                """
                SELECT country_iso2, score_12m, score_3m, friction_score,
                       order_score, information_score, edge_score,
                       condition_flags, evidence_coverage
                  FROM risk_snapshot WHERE as_of = %s ORDER BY country_iso2
                """,
                (run_date,),
            )
            snapshots = {
                r[0]: {"score_12m": r[1], "score_3m": r[2], "friction": r[3],
                       "order": r[4], "information": r[5], "edge": r[6],
                       "condition_flags": r[7], "evidence_coverage": r[8]}
                for r in cur.fetchall()
            }
    finally:
        conn.close()
    return {"run_date": run_date, "census": census, "snapshots": snapshots}


def country_table(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per country: pool, pass rate, selected, ledgers at zero."""
    rows = []
    for iso2, c in sorted(run["census"].items()):
        art = c["articles"] or {}
        ind = c["indicators"] or {}
        deduped = art.get("after_dedupe", 0)
        passed = art.get("passed_gate", 0)
        rows.append({
            "iso2": iso2,
            "name": constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2),
            "fetched": art.get("fetched", 0),
            "after_dedupe": deduped,
            "passed_gate": passed,
            "pass_rate": (passed / deduped) if deduped else 0.0,
            "selected": art.get("selected", 0),
            "budget": art.get("budget", 20),
            "resolved": sum((ind.get("resolved_by_ledger") or {}).values()),
            "expected": sum((ind.get("expected_by_ledger") or {}).values()),
            "empty_ledgers": ind.get("empty_ledgers") or [],
            "ledgers_no_articles": [
                led for led in constants.LEDGERS
                if not (art.get("per_ledger_selected") or {}).get(led)
            ],
            "coverage": c["evidence_coverage"],
        })
    return rows


def rejected_publishers(run: Dict[str, Any], top: int = 20) -> List[Any]:
    """Which publishers the gate turned away most, roster-wide."""
    counter: Counter = Counter()
    by_label: Dict[str, Counter] = defaultdict(Counter)
    for c in run["census"].values():
        for row in (c["articles"] or {}).get("rejected", []):
            pub = (row.get("publisher") or "(unknown)").strip() or "(unknown)"
            counter[pub] += 1
            by_label[pub][row.get("label", "?")] += 1
    return [(pub, n, dict(by_label[pub])) for pub, n in counter.most_common(top)]


def noise_that_got_through(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Selected articles whose title trips the blunt noise net.

    Every hit needs reading before it is called a miss: the net catches
    "royal decree" and "strike a deal" as readily as it catches a football
    result.
    """
    hits = []
    for iso2, c in sorted(run["census"].items()):
        art = c["articles"] or {}
        labels = {row.get("url"): row for row in art.get("gate_labels", [])}
        if not labels:
            continue
        # `gate_labels` carries the verdict, not the headline, so the titles
        # come from the stored articles.
        stored = {r["url"]: r for r in store.read_articles(iso2)}
        for url, row in labels.items():
            if row.get("label") != "structural":
                continue
            article = stored.get(url) or {}
            title = (article.get("title") or "").strip()
            if not title:
                continue
            low = title.lower()
            word = next((w for w in NOISE_WORDS if w in low), None)
            if word:
                hits.append({
                    "iso2": iso2, "title": title, "trigger": word, "url": url,
                    "publisher": article.get("publisher") or "",
                    "body": article.get("body") or "",
                    "reason": row.get("reason", ""),
                })
    return hits


def reason_for(iso2: str, url: str, title: str, publisher: str, body: str) -> str:
    """Recover the gate's reason for one article.

    Prefers the cache: `classify` recomputes the content hash and, if the stored
    body is byte-identical to what the gate read, returns the original verdict
    for nothing. Where it is not — title-only articles whose snippet was not
    stored the same way — this re-asks the model, which is a fresh judgement and
    not the original. The caller is told which by the `cached` flag.
    """
    item = {"title": title, "source": publisher, "text": body or "",
            "publisher_link": url}
    got = relevance.classify(
        [item], constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2), iso2
    )
    verdict = got.get(relevance.relevance_key(item, iso2)) or {}
    mark = "" if verdict.get("cached") else "  [re-asked, not the original call]"
    return (verdict.get("reason", "(no reason recovered)") + mark)


def round_number_shares(run: Dict[str, Any]) -> Dict[str, Any]:
    """How often each returned score is a multiple of five."""
    fields = ("score_12m", "score_3m") + tuple(ai_constants.LEDGER_FIELDS)
    out: Dict[str, Any] = {}
    for field in fields:
        values = [s[field] for s in run["snapshots"].values() if s.get(field) is not None]
        if not values:
            out[field] = {"n": 0}
            continue
        round5 = sum(1 for v in values if v % 5 == 0)
        round10 = sum(1 for v in values if v % 10 == 0)
        out[field] = {
            "n": len(values),
            "multiple_of_5": round5,
            "share_of_5": round5 / len(values),
            "multiple_of_10": round10,
            "mean": sum(values) / len(values),
        }
    return out


def _fmt_table(rows: Sequence[Dict[str, Any]]) -> List[str]:
    lines = [
        "| country | fetched | deduped | passed | pass rate | selected | indicators | ledgers w/ no articles | coverage |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for r in rows:
        empty = ", ".join(r["ledgers_no_articles"]) or "—"
        lines.append(
            f"| {r['iso2']} {r['name']} | {r['fetched']} | {r['after_dedupe']} | "
            f"{r['passed_gate']} | {r['pass_rate']:.0%} | "
            f"{r['selected']}/{r['budget']} | {r['resolved']}/{r['expected']} | "
            f"{empty} | {r['coverage']} |"
        )
    return lines


def report(run_date: Optional[dt.date] = None) -> Dict[str, Any]:
    run = load_run(run_date)
    rows = country_table(run)
    if not rows:
        print("No census rows for that date — has the run finished?")
        return {}

    lines: List[str] = []
    w = lines.append
    w(f"# Roster-wide run — {run['run_date']}")
    w("")
    w(f"{len(rows)} countries with a census; "
      f"{len(run['snapshots'])} with a score.")
    w("")
    w("## Per country")
    w("")
    lines.extend(_fmt_table(rows))
    w("")

    pool = sum(r["after_dedupe"] for r in rows)
    passed = sum(r["passed_gate"] for r in rows)
    w(f"**Roster totals:** {sum(r['fetched'] for r in rows)} fetched, "
      f"{pool} after dedupe, {passed} passed the gate "
      f"({passed / pool:.1%}), {sum(r['selected'] for r in rows)} selected.")
    w("")
    thin = [r for r in rows if r["selected"] < 20]
    w(f"**Scoring on fewer than the full budget of 20:** {len(thin)} countries — "
      + ", ".join(f"{r['iso2']} ({r['selected']})" for r in thin))
    w("")
    zero_ind = [r for r in rows if r["empty_ledgers"]]
    w(f"**Ledgers resolving no indicators:** "
      + (", ".join(f"{r['iso2']} ({'/'.join(r['empty_ledgers'])})" for r in zero_ind)
         or "none"))
    w("")
    zero_art = [r for r in rows if r["ledgers_no_articles"]]
    w(f"**Ledgers with no selected articles:** {len(zero_art)} countries — "
      + ", ".join(f"{r['iso2']} ({'/'.join(r['ledgers_no_articles'])})" for r in zero_art))
    w("")

    w("## Most-rejected publishers, roster-wide")
    w("")
    w("| publisher | rejected | by label |")
    w("|---|---:|---|")
    for pub, n, labels in rejected_publishers(run):
        w(f"| {pub} | {n} | {labels} |")
    w("")

    w("## Round-number shares")
    w("")
    w("| field | n | multiple of 5 | share | multiple of 10 | mean |")
    w("|---|---:|---:|---:|---:|---:|")
    for field, s in round_number_shares(run).items():
        if not s.get("n"):
            w(f"| {field} | 0 | | | | |")
            continue
        w(f"| {field} | {s['n']} | {s['multiple_of_5']} | {s['share_of_5']:.0%} | "
          f"{s['multiple_of_10']} | {s['mean']:.1f} |")
    w("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"[roster] wrote {OUT} ({len(rows)} countries)")

    noise = noise_that_got_through(run)
    print(f"[roster] {len(noise)} selected titles trip the noise net "
          f"(each needs reading before it is called a miss)")
    return {"run": run, "rows": rows, "noise": noise,
            "round_numbers": round_number_shares(run)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default="", help="run date, default today")
    args = ap.parse_args()
    d = dt.date.fromisoformat(args.date) if args.date else None
    report(d)


if __name__ == "__main__":
    main()
