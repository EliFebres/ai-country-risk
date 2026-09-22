"""
Which model should run the relevance gate — measured, not assumed.

The gate sits upstream of everything. If the same article gets a different label
on a re-run, the evidence set changes and every score downstream moves for no
reason at all. So the choice is decided on **label stability**, not on price.

Cost does not decide it: a few hundred candidate articles at a few hundred
tokens each is well under a dollar a run on any of the three candidates. What
separates them is whether they answer the same way twice. Structural-versus-
incident is a judgement, and the cheapest tier is where a judgement is most
likely to wobble — so the cheapest model is exactly the one that has to prove
itself rather than being assumed.

Each candidate model classifies the same articles three times, at temperature 0.
The report gives, per model:

  * **stability** — the share of articles that got the same label all three times
  * **agreement** — how often each model agrees with each other model
  * **cost** — metered, not estimated

The half this cannot answer yet is accuracy: stability without accuracy is a
model that is reliably wrong. Part 7's hand-labelled sample settles that, and
this script writes its per-article labels out so the two can be joined.

    python -m backend.llm.gate_bakeoff [--countries PT,DE,NG] [--n 100]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from backend.llm import relevance
from backend.news_fetching import core
from backend.util import constants, paths, usage

CANDIDATE_MODELS = (
    "gpt-4o-mini-2024-07-18",
    "gpt-4.1-mini-2025-04-14",
    "gpt-4.1-nano-2025-04-14",
)

REPEATS = 3

# Rough per-call shape, for the projection printed before anything is spent.
# The prompt is about 800 tokens and the article slice about 1,000.
EST_INPUT_TOKENS = 1_800
EST_OUTPUT_TOKENS = 80

# Session A's share of the $20 cap. The gate bake-off is the first paid step.
CAP_USD = 5.0


def gather(countries: List[str], n: int) -> List[Dict[str, Any]]:
    """Fetch a candidate pool across a few countries. Free — RSS and scraping."""
    pool: List[Dict[str, Any]] = []
    names = constants.COUNTRY_NAME_BY_ISO2
    per_country = max(1, n // max(1, len(countries)))

    for iso2 in countries:
        name = names.get(iso2)
        if not name:
            print(f"  {iso2}: not in the roster, skipped")
            continue
        got = core.fetch_candidates(name, iso2)
        print("  " + core.format_pool_report(got["report"]))
        for article in got["items"][:per_country]:
            article["_iso2"] = iso2
            article["_country"] = name
            pool.append(article)

    return pool[:n]


def run(countries: List[str], n: int, out_dir: Path) -> Dict[str, Any]:
    print(f"Gathering candidates from {', '.join(countries)} ...")
    pool = gather(countries, n)
    if not pool:
        print("No candidates fetched; nothing to measure.")
        return {}
    print(f"\n{len(pool)} articles in the sample.\n")

    total_calls = len(pool) * REPEATS * len(CANDIDATE_MODELS)
    projected = 0.0
    for model in CANDIDATE_MODELS:
        projected += usage.project(
            f"gate bake-off, {model}",
            model=model,
            calls=len(pool) * REPEATS,
            input_tokens_per_call=EST_INPUT_TOKENS,
            output_tokens_per_call=EST_OUTPUT_TOKENS,
        )
    print(f"[cost] {total_calls} calls in total, projected ${projected:.4f}, "
          f"cap ${CAP_USD:.2f}\n")
    if projected > CAP_USD:
        raise usage.BudgetExhausted(
            f"bake-off projects ${projected:.2f}, over the ${CAP_USD:.2f} cap"
        )

    by_country: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a in pool:
        by_country[a["_iso2"]].append(a)

    meter = usage.Meter()
    # {model: {relevance_key: [label, label, label]}}
    runs: Dict[str, Dict[str, List[str]]] = {m: defaultdict(list) for m in CANDIDATE_MODELS}
    # {model: {relevance_key: last full payload}}
    payloads: Dict[str, Dict[str, Any]] = {m: {} for m in CANDIDATE_MODELS}

    for model in CANDIDATE_MODELS:
        print(f"--- {model} ---")
        for r in range(REPEATS):
            for iso2, articles in by_country.items():
                got = relevance.classify(
                    articles,
                    articles[0]["_country"],
                    iso2,
                    model=model,
                    meter=meter,
                    use_cache=False,   # measuring the model, not the cache
                )
                for key, verdict in got.items():
                    runs[model][key].append(verdict["label"])
                    payloads[model][key] = verdict
            meter.check(CAP_USD)
            print(f"  repeat {r + 1}/{REPEATS} done — {meter.summary()}")

    report = _report(pool, runs, payloads, meter)
    _write(report, pool, runs, payloads, out_dir)
    return report


def _report(pool, runs, payloads, meter) -> Dict[str, Any]:
    keys_by_article = {
        relevance.relevance_key(a, a["_iso2"]): a for a in pool
    }

    print("\n=== Label stability (same label on all three repeats) ===")
    stability: Dict[str, Any] = {}
    for model in CANDIDATE_MODELS:
        complete = {k: v for k, v in runs[model].items() if len(v) == REPEATS}
        if not complete:
            print(f"{model:<28} no complete results")
            continue
        stable = sum(1 for v in complete.values() if len(set(v)) == 1)
        share = stable / len(complete)
        dist = Counter(v[0] for v in complete.values())
        stability[model] = {
            "n": len(complete),
            "stable": stable,
            "share": share,
            "first_run_labels": dict(dist),
        }
        print(f"{model:<28} {stable}/{len(complete)} = {share:6.1%}   "
              f"first-run labels: {dict(dist)}")

    print("\n=== Cross-model agreement (first repeat) ===")
    agreement: Dict[str, float] = {}
    for i, a in enumerate(CANDIDATE_MODELS):
        for b in CANDIDATE_MODELS[i + 1:]:
            shared = [k for k in runs[a] if k in runs[b] and runs[a][k] and runs[b][k]]
            if not shared:
                continue
            same = sum(1 for k in shared if runs[a][k][0] == runs[b][k][0])
            agreement[f"{a} vs {b}"] = same / len(shared)
            print(f"{a} vs {b}: {same}/{len(shared)} = {same / len(shared):.1%}")

    print("\n=== Disagreements worth reading ===")
    shown = 0
    for key, article in keys_by_article.items():
        labels = {m: runs[m].get(key, [None])[0] for m in CANDIDATE_MODELS}
        if len(set(labels.values())) > 1 and shown < 10:
            shown += 1
            print(f"\n  {article.get('title')}  [{article.get('source')}] ({article['_iso2']})")
            for m in CANDIDATE_MODELS:
                p = payloads[m].get(key, {})
                print(f"    {m:<26} {labels[m]:<12} {p.get('reason', '')[:110]}")

    print(f"\n{meter.summary()}")

    winner = max(stability, key=lambda m: stability[m]["share"]) if stability else None
    if winner:
        best = stability[winner]["share"]
        ties = [m for m in stability if abs(stability[m]["share"] - best) < 0.02]
        if relevance.DEFAULT_MODEL in ties:
            winner = relevance.DEFAULT_MODEL   # a wash keeps the default
        print(f"\nMost stable: {winner} ({stability[winner]['share']:.1%})")
        print("Stability is not accuracy. Part 7's hand labels decide whether the "
              "stable model is also right.")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(pool),
        "repeats": REPEATS,
        "stability": stability,
        "agreement": agreement,
        "winner_on_stability": winner,
        "spend_usd": meter.spend_usd,
    }


def _write(report, pool, runs, payloads, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "gate_bakeoff_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    rows = []
    for a in pool:
        key = relevance.relevance_key(a, a["_iso2"])
        rows.append({
            "relevance_key": key,
            "country_iso2": a["_iso2"],
            "title": a.get("title"),
            "publisher": a.get("source"),
            "url": a.get("publisher_link") or a.get("link"),
            "published": a.get("published"),
            "themes": a.get("themes", []),
            "opening": (a.get("text") or a.get("summary") or "")[:600],
            "labels": {m: runs[m].get(key, []) for m in CANDIDATE_MODELS},
            "reasons": {m: payloads[m].get(key, {}).get("reason") for m in CANDIDATE_MODELS},
        })
    (out_dir / "gate_bakeoff_articles.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {out_dir / 'gate_bakeoff_report.json'}")
    print(f"Wrote {out_dir / 'gate_bakeoff_articles.json'}  "
          f"({len(rows)} articles, for Part 7's hand labelling)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--countries", default="US,PT,KW",
                    help="comma-separated ISO-2 codes to draw the sample from")
    ap.add_argument("--n", type=int, default=100, help="sample size")
    ap.add_argument("--out", default="", help="output directory")
    args = ap.parse_args()

    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    out_dir = Path(args.out) if args.out else paths.PROJECT_ROOT / "bakeoff_out"
    run(countries, args.n, out_dir)


if __name__ == "__main__":
    main()
