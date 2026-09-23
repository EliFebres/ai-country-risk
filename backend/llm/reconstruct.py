"""
Rebuild a scored week from its manifest alone, and see whether it still holds.

This is the test the record exists to pass. A score is worth keeping only if
someone can come back later and establish what it was made from — which
articles, which numbers, which versions — without the run that produced it still
being around. If the manifest cannot be turned back into the payload, the row is
a number with a story attached rather than evidence.

The rule the check enforces: **everything comes from the manifest and the
tables**. Nothing is re-fetched, nothing is recomputed from today's registry,
and no value is taken from the run. Where the manifest names an article, the
article must be in `article`; where it names an indicator at a period and an
`as_of`, that exact row must be in `indicator_series`.

    python -m backend.llm.reconstruct --country PT
"""

from __future__ import annotations

import argparse
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from backend.data_upsert import data_push, store
from backend.llm import constants as ai_constants
from backend.util import constants, db

__all__ = ["reconstruct", "report"]


def _check(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def reconstruct(iso2: str, run_date: dt.date) -> Dict[str, Any]:
    """Rebuild one country's payload from its stored manifest.

    Returns:
        ``{"checks": [...], "ok": bool, "rebuilt": {...}}``.
    """
    snapshot = store.read_snapshot(iso2, run_date)
    if not snapshot:
        return {"ok": False, "checks": [_check("snapshot exists", False,
                                               f"no row for {iso2} {run_date}")],
                "rebuilt": {}}

    manifest = snapshot.get("manifest") or {}
    census = manifest.get("census") or {}
    checks: List[Dict[str, Any]] = [_check("snapshot exists", True)]

    checks.append(_check(
        "manifest present and non-empty",
        bool(manifest), f"{len(manifest)} top-level keys"
    ))

    # --- the articles ------------------------------------------------------
    selected = manifest.get("selected") or []
    stored = {a["url"]: a for a in store.read_articles(iso2)}
    found = [s for s in selected if s.get("url") in stored]
    missing = [s.get("url") for s in selected if s.get("url") not in stored]
    checks.append(_check(
        "every selected article is still in `article`",
        not missing,
        f"{len(found)}/{len(selected)} found"
        + (f"; missing {missing[:3]}" if missing else ""),
    ))

    body_mismatch = [
        s["url"] for s in found_pairs(selected, stored)
        if s is not None
    ]
    checks.append(_check(
        "stored body_status matches the manifest",
        not body_mismatch,
        f"{len(body_mismatch)} disagree" if body_mismatch else "all agree",
    ))

    # --- the indicators ----------------------------------------------------
    wanted = manifest.get("indicators") or []
    series = store.read_indicator_series(iso2)
    have = {(r["indicator_code"], r["as_of"].isoformat()) for r in series}
    absent = [
        f"{w['code']}@{w['as_of']}" for w in wanted
        if (w["code"], str(w["as_of"])[:10]) not in have
    ]
    checks.append(_check(
        "every manifest indicator is in `indicator_series` at its own vintage",
        not absent,
        f"{len(wanted) - len(absent)}/{len(wanted)} found"
        + (f"; missing {absent[:3]}" if absent else ""),
    ))

    # --- the versions ------------------------------------------------------
    checks.append(_check(
        "prompt hash recorded",
        bool(snapshot.get("prompt_version")),
        str(snapshot.get("prompt_version"))[:16],
    ))
    checks.append(_check(
        "prompt hash still matches the prompt in the tree",
        snapshot.get("prompt_version") == ai_constants.PROMPT_VERSION,
        "unchanged since the run" if snapshot.get("prompt_version") ==
        ai_constants.PROMPT_VERSION else
        "THE PROMPT HAS CHANGED — this week is a step in the series",
    ))
    for field in ("scoring_model", "digest_model", "gate_model", "git_sha",
                  "payload_fingerprint", "seed", "payload_tokens"):
        checks.append(_check(f"{field} recorded", snapshot.get(field) is not None,
                            str(snapshot.get(field))[:40]))

    # --- does the reconstruction agree with the census? --------------------
    c_articles = census.get("articles") or {}
    c_indicators = census.get("indicators") or {}
    checks.append(_check(
        "manifest article count matches the census",
        len(selected) == (c_articles.get("selected") or 0),
        f"manifest {len(selected)} vs census {c_articles.get('selected')}",
    ))
    resolved_total = sum((c_indicators.get("resolved_by_ledger") or {}).values())
    checks.append(_check(
        "manifest indicator count matches the census",
        len(wanted) == resolved_total,
        f"manifest {len(wanted)} vs census {resolved_total}",
    ))
    checks.append(_check(
        "coverage column matches the census",
        snapshot.get("evidence_coverage") == census.get("evidence_coverage"),
        f"column {snapshot.get('evidence_coverage')} vs census "
        f"{census.get('evidence_coverage')}",
    ))
    checks.append(_check(
        "rejected articles kept with labels and reasons",
        all(r.get("label") and r.get("reason") for r in (manifest.get("rejected") or [])),
        f"{len(manifest.get('rejected') or [])} rejections recorded",
    ))

    rebuilt = {
        "country": iso2,
        "run_date": str(run_date),
        "articles": [
            {"id": s.get("id"), "title": stored.get(s.get("url"), {}).get("title"),
             "publisher": stored.get(s.get("url"), {}).get("publisher"),
             "body_status": s.get("body_status")}
            for s in selected
        ],
        "indicators": wanted,
        "versions": {
            f: snapshot.get(f) for f in
            ("prompt_version", "scoring_model", "digest_model", "gate_model",
             "git_sha", "seed", "payload_tokens", "payload_fingerprint")
        },
    }
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "rebuilt": rebuilt}


def found_pairs(selected, stored):
    """Manifest entries whose stored `body_status` disagrees with the manifest."""
    out = []
    for s in selected:
        row = stored.get(s.get("url"))
        if row and s.get("body_status") and row.get("body_status") != s["body_status"]:
            out.append(s)
    return out


def report(iso2: str, run_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """Run the reconstruction and print it."""
    db.announce()
    run_date = run_date or data_push.read_latest_snapshot_date()
    if run_date is None:
        print("no scored runs to reconstruct")
        return {}

    name = constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2)
    got = reconstruct(iso2, run_date)

    print("")
    print(f"=== Reconstruction — {name} ({iso2}), run {run_date} ===")
    print("")
    for c in got["checks"]:
        mark = "ok  " if c["ok"] else "FAIL"
        print(f"  [{mark}] {c['check']}" + (f"  — {c['detail']}" if c["detail"] else ""))
    print("")
    rebuilt = got.get("rebuilt") or {}
    if rebuilt:
        print(f"  rebuilt from the manifest alone: {len(rebuilt['articles'])} articles, "
              f"{len(rebuilt['indicators'])} indicators")
        for a in rebuilt["articles"][:5]:
            print(f"    {a['id']:<4} [{a['body_status']:<11}] "
                  f"{(a['publisher'] or '')[:22]:<22} {(a['title'] or '')[:64]}")
        if len(rebuilt["articles"]) > 5:
            print(f"    ... and {len(rebuilt['articles']) - 5} more")
    print("")
    print(f"  => {'PASS' if got['ok'] else 'FAIL'}")
    return got


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--country", default="PT")
    ap.add_argument("--date", default="")
    args = ap.parse_args()
    report(args.country.upper(),
           dt.date.fromisoformat(args.date) if args.date else None)


if __name__ == "__main__":
    main()
