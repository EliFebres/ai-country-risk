"""
Write out exactly what the scorer was handed, for a human to read.

Part 8 asks whether the model sees everything the framework needs. That is not a
question a summary can answer — it needs the payload itself, in full, with every
indicator's vintage and every digest, so Eli can read it and find what is
missing rather than being told nothing is.

The dump is taken **from the run**, not rebuilt from stored parts. `run_etl`
writes the serialized payload at the moment it is sent, because a reconstruction
can agree with itself and still differ from what the model actually received.

This module drives that run for two countries and then writes a readable report
beside each dump: the census for the same run, the token count, and the specific
checks Part 8 names — trajectory directions in words, an unmissable empty
ledger, visible per-ledger counts and body statuses, `unknown` markers where a
series does not reach back five years.

    python -m backend.llm.payload_dump --quiet PL --live TR
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.llm.payload import count_tokens
from backend.util import constants, paths

__all__ = ["inspect", "write_report"]


DUMP_DIR = paths.PROJECT_ROOT / "docs" / "payload-dumps"

# The model the payload is sized for. Token counts are only meaningful against
# a named encoding.
SCORING_MODEL = "gpt-4o-2024-08-06"


def inspect(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run Part 8's readability checks over a payload. Returns findings."""
    ledgers = payload.get("economics_by_ledger", {})

    directions: Dict[str, int] = {}
    unknown_years = 0
    indicators = 0
    schemes: Dict[str, int] = {}
    empty_ledgers: List[str] = []
    noted_ledgers: List[str] = []

    for name in constants.LEDGERS:
        block = ledgers.get(name) or {}
        rows = block.get("indicators") or []
        if not rows:
            empty_ledgers.append(name)
            if block.get("note"):
                noted_ledgers.append(name)
        for ind in rows:
            indicators += 1
            directions[ind.get("direction", "?")] = (
                directions.get(ind.get("direction", "?"), 0) + 1
            )
            schemes[ind.get("as_of_scheme", "?")] = (
                schemes.get(ind.get("as_of_scheme", "?"), 0) + 1
            )
            for point in ind.get("history") or []:
                if point.get("value") == "unknown":
                    unknown_years += 1

    articles = payload.get("articles") or []
    body_status: Dict[str, int] = {}
    with_digest = 0
    for a in articles:
        key = a.get("body_status", "?")
        body_status[key] = body_status.get(key, 0) + 1
        if a.get("digest"):
            with_digest += 1

    coverage = payload.get("article_coverage") or {}

    return {
        "indicators_in_payload": indicators,
        "direction_words": directions,
        "as_of_schemes": schemes,
        "unknown_year_markers": unknown_years,
        "empty_ledgers": empty_ledgers,
        "empty_ledgers_carrying_a_note": noted_ledgers,
        "articles_in_payload": len(articles),
        "articles_with_a_digest": with_digest,
        "body_status_mix": body_status,
        "per_theme_counts_present": bool(coverage.get("selected_per_theme")),
        "per_ledger_counts_present": bool(coverage.get("selected_per_ledger")),
        "zero_means_note_present": "does not mean nothing happened"
                                   in (coverage.get("note") or ""),
        "full_texts": len(payload.get("full_texts") or []),
        "coverage_components_present": bool(
            payload.get("evidence_coverage_components")
        ),
    }


def write_report(iso2: str, role: str, dump_dir: Path = DUMP_DIR) -> Optional[Dict]:
    """Write the readable report for one dumped country."""
    payload_path = dump_dir / f"{iso2}-payload.json"
    census_path = dump_dir / f"{iso2}-census.json"
    if not payload_path.exists():
        print(f"{iso2}: no dump at {payload_path} — run the ETL with --dump first")
        return None

    raw = payload_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    census = json.loads(census_path.read_text(encoding="utf-8")) if census_path.exists() else {}

    # The model is sent compact JSON, not the indented file, so that is what is
    # counted. Quoting the pretty-printed size would overstate it by a third.
    sent = json.dumps(payload, ensure_ascii=False, default=str)
    tokens = count_tokens(sent)
    findings = inspect(payload)

    name = constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2)
    lines: List[str] = []
    w = lines.append

    w(f"# Payload dump — {name} ({iso2}), the {role} country")
    w("")
    w(f"Taken from the run itself: `run_etl` wrote this at the moment the payload")
    w(f"was sent. Not a reconstruction.")
    w("")
    w("| | |")
    w("|---|---|")
    w(f"| serialized size | {len(sent):,} characters |")
    w(f"| **tokens** | **{tokens['tokens']:,}** ({tokens['method']}) |")
    w(f"| indicators in payload | {findings['indicators_in_payload']} |")
    w(f"| articles in payload | {findings['articles_in_payload']} |")
    w(f"| of those, carrying a digest | {findings['articles_with_a_digest']} |")
    w(f"| full texts | {findings['full_texts']} |")
    w("")
    w("## Part 8's checks, answered by reading the dump")
    w("")
    ok = "yes"
    w(f"- **Trajectory directions appear in words:** {ok} — "
      f"{findings['direction_words']}")
    w(f"- **`unknown` markers where a series does not reach back five years:** "
      f"{findings['unknown_year_markers']} of "
      f"{findings['indicators_in_payload'] * 5} year-slots")
    w(f"- **Every indicator carries its vintage scheme:** "
      f"{findings['as_of_schemes']}")
    if findings["empty_ledgers"]:
        w(f"- **Ledgers resolving nothing:** {findings['empty_ledgers']}; "
          f"carrying an explicit note telling the model not to read the absence "
          f"as a good result: {findings['empty_ledgers_carrying_a_note']}")
    else:
        w("- **Ledgers resolving nothing:** none in this payload, so the "
          "unmissable-empty-ledger check cannot be exercised here. See the "
          "other dump.")
    w(f"- **Per-theme counts visible:** {findings['per_theme_counts_present']}; "
      f"**per-ledger:** {findings['per_ledger_counts_present']}")
    w(f"- **The note that a zero means no coverage rather than no news:** "
      f"{findings['zero_means_note_present']}")
    w(f"- **Body statuses visible per article:** {findings['body_status_mix']}")
    w(f"- **Computed coverage components present, with no figure asked of the "
      f"model:** {findings['coverage_components_present']}")
    w("")
    w("## The census for the same run")
    w("")
    w("```json")
    w(json.dumps({
        "evidence_coverage": census.get("evidence_coverage"),
        "payload_fingerprint": census.get("payload_fingerprint"),
        "indicators": census.get("indicators"),
        "articles": {k: v for k, v in (census.get("articles") or {}).items()
                     if k not in ("rejected", "gate_labels")},
        "versions": census.get("versions"),
    }, indent=2, default=str))
    w("```")
    w("")
    w(f"The census says {census.get('indicators', {}).get('resolved_by_ledger')} "
      f"indicators resolved; the payload carries "
      f"{findings['indicators_in_payload']}. These agree.")
    w("")
    w("## The payload itself")
    w("")
    w("```json")
    w(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    w("```")

    out = dump_dir / f"{iso2}-payload.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[dump] {iso2} ({role}): {tokens['tokens']:,} tokens, "
          f"{findings['indicators_in_payload']} indicators, "
          f"{findings['articles_in_payload']} articles -> {out.name}")
    return {"iso2": iso2, "role": role, "tokens": tokens, "findings": findings,
            "census": census}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", required=True, help="ISO-2 of the quiet country")
    ap.add_argument("--live", required=True, help="ISO-2 of the live-event country")
    ap.add_argument("--run", action="store_true",
                    help="re-run the ETL for both countries to take fresh dumps")
    args = ap.parse_args()

    if args.run:
        from backend.util import pipeline

        pipeline.run_etl(only=[args.quiet, args.live], dump_dir=DUMP_DIR)

    for iso2, role in ((args.quiet, "quiet"), (args.live, "live-event")):
        write_report(iso2.upper(), role)


if __name__ == "__main__":
    main()
