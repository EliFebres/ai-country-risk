"""
Run the relevance gate alone over the test set: retrieval and the gate, no
digest and no scorer.

This is what an adopted gate is checked with before it scores anything. For each
country it reports the candidates, how many the gate admitted and rejected, the
high-impact events, which test admitted each article (T1 to T5) and which
exclusion rejected it (E1 to E8), and five rejects with their reasons. Verdicts
are cached exactly as a real run would cache them.

    python -m backend.llm.gate_run [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from backend.llm import digest_engine, relevance
from backend.util import constants, pipeline, usage

__all__ = ["run"]

SAMPLE_REJECTS = 5
SEED = 20260927


def run(countries: List[str], cap_usd: float = 1.0) -> Dict[str, Any]:
    meter = usage.Meter()
    report: Dict[str, Any] = {
        "gate_model": relevance.DEFAULT_MODEL,
        "gate_input_mode": relevance.DEFAULT_INPUT_MODE,
        "relevance_prompt_version": relevance.RELEVANCE_PROMPT_VERSION,
        "exposure_cards_version": relevance.EXPOSURE_CARDS_VERSION,
        "countries": {},
    }
    rng = random.Random(SEED)
    for iso2 in countries:
        name = constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2)
        candidates, _ = pipeline._fetch_candidate_pool(name, iso2)
        # As in the pipeline: a body a previous digest called `not_article` is
        # read as a snippet.
        known = digest_engine.cached_body_quality(candidates)
        for c in candidates:
            q = known.get(c.get("publisher_link") or c.get("link"))
            if q:
                c["body_quality"] = q
        usage.project(f"gate {iso2}", model=relevance.DEFAULT_MODEL, calls=len(candidates),
                      input_tokens_per_call=4200, output_tokens_per_call=130,
                      cap_usd=cap_usd - meter.spend_usd)
        labels = relevance.classify(candidates, name, iso2, meter=meter)
        gate = relevance.select(candidates, labels, iso2)
        meter.check(cap_usd)

        rows = gate["gate_labels"]
        admitted = [r for r in rows if r["label"] == "relevant"]
        rejected = [r for r in rows if r["label"] != "relevant"]
        titles = {c.get("publisher_link") or c.get("link"): c.get("title") for c in candidates}
        sample = rng.sample(rejected, min(SAMPLE_REJECTS, len(rejected)))
        report["countries"][iso2] = {
            "candidates": len(candidates),
            "admitted": len(admitted),
            "rejected": len(rejected),
            "selected": gate["counts"]["selected"],
            "unclassified": sum(1 for r in rows if r["label"] == "unclassified"),
            "high_impact_event": sum(1 for r in admitted if r.get("high_impact_event")),
            "by_test": dict(Counter(r["test"] for r in admitted)),
            "by_exclusion": dict(Counter(r["exclusion"] for r in rejected if r.get("exclusion"))),
            "rejected_no_test": sum(1 for r in rejected if r.get("exclusion") == "none"),
            "per_ledger": gate["per_ledger"],
            "sample_rejects": [
                {"title": titles.get(r["url"]), "exclusion": r.get("exclusion"),
                 "test": r.get("test"), "reason": r.get("reason")}
                for r in sample
            ],
        }
        c = report["countries"][iso2]
        print(f"[gate] {iso2}: {c['candidates']} candidates, {c['admitted']} admitted, "
              f"{c['rejected']} rejected, {c['high_impact_event']} high-impact")
    report["spend_usd"] = meter.spend_usd
    report["calls"] = meter.calls
    print(f"[gate] {meter.summary()}")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, help="JSON file for the full report")
    ap.add_argument("--cap-usd", type=float, default=1.0)
    args = ap.parse_args()
    report = run(list(constants.TEST_SET), args.cap_usd)
    if args.out:
        args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"[gate] report written to {args.out}")


if __name__ == "__main__":
    main()
