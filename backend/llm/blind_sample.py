"""
Build the blind labelling sample — and say nothing about the labels.

Part 7 asks whether the gate draws the structural-versus-incident line where Eli
draws it. That is a question about agreement with a human, and it can only be
asked of articles nobody has seen a classifier's verdict on.

**This script prints counts and nothing else.** Not a label, not a reason, not a
title next to a verdict. It writes two files: a CSV with no classifier output in
it, and a sealed key it never displays. Whoever runs it can then label the CSV
without having been told the answers.

That constraint is not theoretical. Session A's bake-off printed ten US articles
with each model's label and reason into the session, and all ten had to be
dropped from consideration — in the end the whole sample was redrawn from
countries that had never been run, because contamination you cannot measure is
worse than contamination you exclude.

**The sample is drawn only from countries never run before Session B.** US, PT
and KW are excluded by name.

Stratified over the gate's own judgements: some every model accepted, some every
model rejected, several sitting on the structural-versus-incident line, and
several where the three candidate models disagreed with each other — the last
being the cases the model decision actually turns on.

    python -m backend.llm.blind_sample
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from backend.data_upsert import store
from backend.llm import relevance
from backend.util import constants, paths, usage

__all__ = ["build"]


# Seen in Session A and therefore not blind for this session.
CONTAMINATED = ("US", "PT", "KW")

# The other two candidates from Session A's bake-off. `gpt-4o-mini`'s verdicts
# already exist for every candidate, from the roster run.
EXTRA_MODELS = ("gpt-4.1-mini-2025-04-14", "gpt-4.1-nano-2025-04-14")

SAMPLE_SIZE = 30

# Countries to run the extra two models over. A spread of regions and tiers,
# fixed rather than random so the draw is reproducible.
DISAGREEMENT_COUNTRIES = ("DE", "BR", "IN", "ID", "ZA", "TR", "PL", "MX")

# Per-country cap on how many candidates go to the extra models, to keep the
# pass cheap.
PER_COUNTRY_CAP = 20

SEED = 20260922

OUT_CSV = paths.PROJECT_ROOT / "docs" / "blind-labels.csv"
OUT_KEY = paths.PROJECT_ROOT / "docs" / "blind-labels-key.json"

OPENING_CHARS = 420


def _clean_countries() -> List[str]:
    return [
        c["iso2"] for c in constants.COUNTRY_ROSTER
        if c["iso2"] not in CONTAMINATED
    ]


def _gather(countries: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Collect stored articles joined to the roster run's gate verdicts.

    Returns ``{url: {article fields, 'baseline_label': ...}}``. Nothing is
    printed.
    """
    out: Dict[str, Dict[str, Any]] = {}
    # Which run to read, not a vintage — the census is keyed on the day it was
    # taken, and we want today's.
    run_date = dt.date.today()

    for iso2 in countries:
        snapshot = store.read_snapshot(iso2, run_date)
        if not snapshot:
            continue
        census = ((snapshot.get("manifest") or {}).get("census")) or {}
        verdicts = {
            row["url"]: row
            for row in (census.get("articles") or {}).get("gate_labels", [])
            if row.get("url")
        }
        if not verdicts:
            continue
        for url, row in store.read_articles(verdicts).items():
            verdict = verdicts[url]
            if not (row.get("title") or "").strip():
                continue
            out[url] = {
                "url": url,
                "country_iso2": iso2,
                "country": constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2),
                "title": (row.get("title") or "").strip(),
                "publisher": (row.get("publisher") or "").strip() or "(unknown)",
                "themes": verdict.get("themes") or [],
                "body": row.get("body") or row.get("abstract") or "",
                "published": str(row.get("page_published_at") or row.get("published_at") or "")[:10],
                "baseline_label": verdict.get("label"),
            }
    return out


def _extra_model_labels(
    pool: Dict[str, Dict[str, Any]],
    meter: usage.Meter,
) -> Dict[str, Dict[str, str]]:
    """Run the other two candidate models. Returns ``{url: {model: label}}``."""
    by_country: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in pool.values():
        if row["country_iso2"] in DISAGREEMENT_COUNTRIES:
            by_country[row["country_iso2"]].append(row)

    out: Dict[str, Dict[str, str]] = defaultdict(dict)
    for model in EXTRA_MODELS:
        for iso2, rows in by_country.items():
            batch = rows[:PER_COUNTRY_CAP]
            # `classify` wants live-shaped items; `text` is what it reads.
            items = [
                {"title": r["title"], "source": r["publisher"], "text": r["body"],
                 "publisher_link": r["url"]}
                for r in batch
            ]
            got = relevance.classify(
                items,
                constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2),
                iso2,
                model=model,
                meter=meter,
                use_cache=False,   # measuring these models, not the cache
            )
            for item, r in zip(items, batch):
                verdict = got.get(relevance.relevance_key(item, iso2))
                if verdict:
                    out[r["url"]][model] = verdict["label"]
    return out


def _stratify(
    pool: Dict[str, Dict[str, Any]],
    extra: Dict[str, Dict[str, str]],
    size: int,
) -> List[Dict[str, Any]]:
    """Pick the sample. Returns rows carrying their stratum and every label."""
    rng = random.Random(SEED)

    enriched = []
    for url, row in pool.items():
        labels = {"gpt-4o-mini-2024-07-18": row["baseline_label"]}
        labels.update(extra.get(url, {}))
        values = [v for v in labels.values() if v]
        entry = {**row, "labels": labels, "n_models": len(values)}

        if len(set(values)) > 1:
            stratum = "models disagreed"
        elif values and set(values) == {"structural"}:
            stratum = "all accepted"
        elif values and "incident" in values:
            stratum = "on the incident line"
        else:
            stratum = "all rejected"
        entry["stratum"] = stratum
        enriched.append(entry)

    # Disagreement cases can only exist where more than one model ran, so they
    # are the scarce stratum and are taken first.
    want = {
        "models disagreed": 9,
        "on the incident line": 8,
        "all accepted": 7,
        "all rejected": 6,
    }

    by_stratum: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in enriched:
        by_stratum[e["stratum"]].append(e)
    for rows in by_stratum.values():
        rng.shuffle(rows)

    picked: List[Dict[str, Any]] = []
    seen_countries: Dict[str, int] = defaultdict(int)

    for stratum, n in want.items():
        rows = by_stratum.get(stratum, [])
        # Spread across countries: prefer one nobody has taken much from yet.
        rows.sort(key=lambda r: seen_countries[r["country_iso2"]])
        for r in rows[:n]:
            picked.append(r)
            seen_countries[r["country_iso2"]] += 1

    # Top up from whatever is left if a stratum came up short.
    if len(picked) < size:
        chosen = {r["url"] for r in picked}
        rest = [e for e in enriched if e["url"] not in chosen]
        rng.shuffle(rest)
        picked.extend(rest[: size - len(picked)])

    picked = picked[:size]
    # Shuffle again so the file's order carries no hint of the stratum.
    rng.shuffle(picked)
    return picked


def build(size: int = SAMPLE_SIZE) -> int:
    """Write the blind CSV and the sealed key. Prints counts only."""
    countries = _clean_countries()
    pool = _gather(countries)
    print(f"[sample] {len(pool)} candidates available "
          f"across {len({r['country_iso2'] for r in pool.values()})} clean countries")
    print(f"[sample] excluded by name as already seen: {', '.join(CONTAMINATED)}")
    if not pool:
        print("[sample] nothing to draw from — has the roster run finished?")
        return 0

    meter = usage.Meter()
    n_extra = sum(
        min(PER_COUNTRY_CAP, sum(1 for r in pool.values() if r["country_iso2"] == c))
        for c in DISAGREEMENT_COUNTRIES
    )
    for model in EXTRA_MODELS:
        usage.project(f"disagreement pass, {model}", model=model, calls=n_extra,
                      input_tokens_per_call=1300, output_tokens_per_call=70)

    extra = _extra_model_labels(pool, meter)
    print(f"[sample] second and third models ran over {len(extra)} articles")
    print(f"[sample] {meter.summary()}")

    picked = _stratify(pool, extra, size)
    counts: Dict[str, int] = defaultdict(int)
    for r in picked:
        counts[r["stratum"]] += 1
    print(f"[sample] drew {len(picked)}: " +
          ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"[sample] countries represented: "
          f"{len({r['country_iso2'] for r in picked})}")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "country", "publisher", "section", "title",
                         "opening_text", "your_label"])
        for i, r in enumerate(picked, start=1):
            opening = " ".join((r["body"] or "").split())[:OPENING_CHARS]
            writer.writerow([
                f"b{i:02d}", r["country"], r["publisher"],
                "/".join(r["themes"]) or "broad",
                r["title"], opening, "",
            ])

    OUT_KEY.write_text(json.dumps({
        "_note": "SEALED. Do not read before the blind labels are committed.",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "seed": SEED,
        "excluded_countries": list(CONTAMINATED),
        "rows": [
            {"id": f"b{i:02d}", "url": r["url"], "country_iso2": r["country_iso2"],
             "title": r["title"], "stratum": r["stratum"], "labels": r["labels"]}
            for i, r in enumerate(picked, start=1)
        ],
    }, indent=2), encoding="utf-8")

    print(f"[sample] wrote {OUT_CSV} ({len(picked)} rows, no classifier output)")
    print(f"[sample] wrote {OUT_KEY} (sealed)")
    return len(picked)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", type=int, default=SAMPLE_SIZE)
    args = ap.parse_args()
    build(args.size)


if __name__ == "__main__":
    main()
