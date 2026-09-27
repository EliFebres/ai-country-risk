"""
Score the relevance gate against Eli's blind labels.

Eli's labels are the definition of correct. This script puts a labelled set to
the gate under one or more (model, input mode) cells and reports agreement,
recall and precision on `relevant`, and what each cell cost.

**It prints counts and nothing else.** The per-article verdicts and reasons go to
a JSON file, so a session that runs it has not been shown a verdict next to a
title (see `blind_sample.py` for why that matters).

Labels are collapsed to binary on the way in: `structural` and `incident` (the
v2 page's three buttons) are `relevant`, `irrelevant` stays `irrelevant`. The v3
page asks the binary question directly.

    python -m backend.llm.gate_eval --labels docs/blind-labels.csv \\
        --key docs/blind-labels-key.json --ids b02,b04,... \\
        --cells gpt-4o-mini-2024-07-18:body --out <file>
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.data_upsert import store
from backend.llm import digest_engine, relevance
from backend.util import constants, usage

__all__ = ["load_set", "run_cell", "score"]

# Tokens per gate call, for the projection before a run: the prompt with its ten
# worked examples is most of it, the article at most 1,500 characters.
EST_INPUT_TOKENS = {"snippet": 2400, "body": 2800}
EST_OUTPUT_TOKENS = 110


def _binary(label: str) -> str:
    return "irrelevant" if label == "irrelevant" else "relevant"


def load_set(labels_csv: Path, key_json: Path, ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """The labelled articles, as the gate would have received them.

    Returns rows with ``id``, ``iso2``, ``truth`` and ``item`` (live-shaped:
    title, source, text, snippet, body_quality).
    """
    with open(labels_csv, encoding="utf-8-sig", newline="") as fh:
        truth = {r["id"].strip(): r["your_label"].strip() for r in csv.DictReader(fh)}
    key = json.loads(Path(key_json).read_text(encoding="utf-8"))
    rows = [r for r in key["rows"] if not ids or r["id"] in set(ids)]

    stored = store.read_articles([r["url"] for r in rows])
    out = []
    for r in rows:
        a = stored.get(r["url"]) or {}
        item = {
            "title": (a.get("title") or r.get("title") or "").strip(),
            "source": (a.get("publisher") or "").strip(),
            "text": a.get("body") or "",
            # The stored abstract is the feed's description, which is what
            # `snippet` mode reads.
            "snippet": a.get("abstract") or "",
            "publisher_link": r["url"],
        }
        out.append({"id": r["id"], "iso2": r["country_iso2"],
                    "truth": _binary(truth[r["id"]]), "item": item})

    quality = digest_engine.cached_body_quality([r["item"] for r in out])
    for r in out:
        q = quality.get(r["item"]["publisher_link"])
        if q:
            r["item"]["body_quality"] = q
    return out


def run_cell(rows: Sequence[Dict[str, Any]], model: str, input_mode: str,
             meter: usage.Meter, cap_usd: float) -> Dict[str, Dict[str, Any]]:
    """Ask the gate about every row. Uncached: this measures the model."""
    verdicts: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        iso2 = r["iso2"]
        got = relevance.classify(
            [r["item"]], constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2), iso2,
            model=model, input_mode=input_mode, meter=meter, use_cache=False,
        )
        verdicts[r["id"]] = got.get(relevance.relevance_key(r["item"], iso2, input_mode)) or {}
        meter.check(cap_usd)
    return verdicts


def score(rows: Sequence[Dict[str, Any]], verdicts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Agreement, and recall and precision on `relevant`. A failed call counts
    as `irrelevant`, which is what the pipeline does with it."""
    n = len(rows)
    agree = tp = fp = fn = 0
    for r in rows:
        said = (verdicts.get(r["id"]) or {}).get("label", "irrelevant")
        agree += said == r["truth"]
        tp += said == "relevant" and r["truth"] == "relevant"
        fp += said == "relevant" and r["truth"] == "irrelevant"
        fn += said == "irrelevant" and r["truth"] == "relevant"
    return {
        "n": n, "agree": agree,
        "recall_relevant": tp / (tp + fn) if tp + fn else None,
        "precision_relevant": tp / (tp + fp) if tp + fp else None,
        "tp": tp, "fp": fp, "fn": fn,
        "failed_calls": sum(1 for r in rows if not verdicts.get(r["id"])),
    }


def _parse_cells(spec: str) -> List[Tuple[str, str]]:
    cells = []
    for part in spec.split(","):
        model, mode = part.strip().rsplit(":", 1)
        usage.price_of(model)          # an unpriced model fails before any call
        if mode not in relevance.INPUT_MODES:
            raise ValueError(f"unknown input mode {mode!r}")
        cells.append((model, mode))
    return cells


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--ids", default="", help="comma-separated subset of ids")
    ap.add_argument("--cells", required=True, help="model:mode,model:mode,...")
    ap.add_argument("--out", required=True, help="JSON file for per-article verdicts")
    ap.add_argument("--cap-usd", type=float, default=2.0)
    args = ap.parse_args()

    ids = [i.strip() for i in args.ids.split(",") if i.strip()] or None
    rows = load_set(Path(args.labels), Path(args.key), ids)
    cells = _parse_cells(args.cells)

    for model, mode in cells:
        usage.project(f"gate eval {model}:{mode}", model=model, calls=len(rows),
                      input_tokens_per_call=EST_INPUT_TOKENS[mode],
                      output_tokens_per_call=EST_OUTPUT_TOKENS, cap_usd=args.cap_usd)

    report: Dict[str, Any] = {"rows": [{k: r[k] for k in ("id", "iso2", "truth")} for r in rows],
                              "prompt_version": relevance.RELEVANCE_PROMPT_VERSION,
                              "cells": {}}
    for model, mode in cells:
        meter = usage.Meter()
        verdicts = run_cell(rows, model, mode, meter, args.cap_usd)
        s = score(rows, verdicts)
        s["spend_usd"] = meter.spend_usd
        s["tokens"] = {"input": meter.input_tokens, "output": meter.output_tokens}
        report["cells"][f"{model}:{mode}"] = {"score": s, "verdicts": verdicts}
        rec = "-" if s["recall_relevant"] is None else f"{s['recall_relevant']:.0%}"
        pre = "-" if s["precision_relevant"] is None else f"{s['precision_relevant']:.0%}"
        print(f"[eval] {model}:{mode}  agree {s['agree']}/{s['n']}  recall {rec}  "
              f"precision {pre}  failed {s['failed_calls']}  ${s['spend_usd']:.4f}")

    Path(args.out).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[eval] verdicts written to {args.out}")


if __name__ == "__main__":
    main()
