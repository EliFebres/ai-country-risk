"""Re-probe stored bundles and diff the result against the last masking behaviour.

The measurement `probe_result` exists to keep. It assembles a historical bundle
exactly the way `pipeline._process_country` does — selector, gazetteer, digest in
masked mode with the sweep, gazetteer again — and asks the cheap model which
country it is reading.

Two bundle sets, and they answer different questions.

``--recorded`` takes the bundles that left a trace in `article_digest`. Their
digests are already cached, so this is the acceptance test for a masking change:
the same text, re-probed, at almost no cost.

``--fresh`` draws a reproducible sample across the pilot roster: per country, the
loudest weeks by |Δ| in article volume and the quietest, because they fail
differently. A uniform draw lands on neither. This is the baseline a future
change gets compared against.

Spend is metered from the API's own usage fields, never estimated.

    python -m backend.scripts.probe_bundles --recorded
    python -m backend.scripts.probe_bundles --fresh --per-country 4
"""

import argparse
import collections
import datetime
import os
import pathlib
import statistics
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# The whole output of this script is names the masking was supposed to remove,
# and the interesting ones are exactly the ones a Windows console cannot encode:
# the first run died on the ğ in "Erdoğan" — mid-loop, after the API calls had
# been paid for and before the diff was printed. Replacing is right rather than
# raising: a probe report is not worth losing to a codec.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / "backend" / ".env")

from backend.utils import pipeline  # noqa: E402
from backend.utils.ai import client as ai_client, digest_engine  # noqa: E402
from backend.utils.data_upsert import data_push  # noqa: E402
from backend.utils.history import config, snapshot_select, store, usage  # noqa: E402
from backend.utils.masking import gazetteer, probe, rewrite  # noqa: E402

# The seed is printed with every fresh sample and is part of the report: a
# baseline nobody can redraw is a baseline nobody can check.
DEFAULT_SEED = 20260812


def recorded_bundles() -> list:
    """Historical (country, as_of) pairs that left a digest-cache trace.

    Anything anchored in the current year is a live daily-run bundle rather than
    a harvested historical one, and is not what the pilot scores.
    """
    with data_push._transaction() as cur:
        cur.execute("""
            SELECT country_iso2, as_of, count(*) FROM article_digest
             WHERE as_of < %s GROUP BY 1, 2 ORDER BY 2, 1
        """, (datetime.date(datetime.date.today().year, 1, 1),))
        return [(c, d, n) for c, d, n in cur.fetchall()]


def fresh_bundles(per_country: int, seed: int) -> list:
    """A reproducible sample spanning loud and quiet weeks, per country.

    Stratified on article volume rather than drawn uniformly. A week whose whole
    story is one named institution is where masking either survives or does not;
    a quiet week is where a thin bundle can be identified from almost nothing,
    which is the Portugal case and the one worth watching. A uniform draw over a
    decade lands mostly on unremarkable weeks and measures neither.

    The rule, stated so the sample can be redrawn: for each country take every
    anchor with at least one article, rank by bundle size, and take the
    ``per_country // 2`` largest and the ``per_country // 2`` smallest, breaking
    ties on the anchor date so the draw does not depend on scan order.
    """
    anchors = snapshot_select_anchors()
    out = []
    for iso2 in config.PILOT_ROSTER:
        sized = []
        for a in anchors:
            start, end = snapshot_select.window(a)
            rows = store.read_window(iso2, start, end)
            if rows:
                sized.append((len(rows), a))
        if not sized:
            continue
        sized.sort(key=lambda pair: (pair[0], pair[1]))
        half = max(1, per_country // 2)
        picked = sized[-half:] + sized[:half]
        out += [(iso2, a, n) for n, a in sorted({(n, a) for n, a in picked},
                                                key=lambda pair: pair[1])]
    return out


def snapshot_select_anchors() -> list:
    """Every pilot anchor, quarterly-thinned so the scan is not a decade of reads."""
    from backend.utils.history import score
    every = score.anchors(datetime.date.fromisoformat(config.PILOT_START),
                          datetime.date.today())
    return [a for a in every if a.day <= 7 and a.month % 3 == 1]


def probe_one(iso2: str, as_of: datetime.date, api_key: str) -> dict:
    """Assemble one bundle the way the scorer would, then probe it.

    The order matters and mirrors `_process_country` exactly: mask, digest in
    masked mode (which sweeps), rewrite the handful of bodies the scorer reads
    whole, then mask again — because the digest is written after the first pass
    and `country_llm_score` masks once more on the way out. Probing the items as
    they stand before that second pass reads digests in a state the model never
    sees.

    The full-text rewrite is not optional here, and leaving it out is the same
    mistake `9547c48` fixed. The probe appends the raw body for every id in
    `fulltext_ids`, and a body carries this week's president by name where the
    gazetteer cannot see him — it masks countries, and a person is not one. Skip
    the rewrite and the probe measures a bundle strictly more identifiable than
    the one that gets sent, which reads as a masking failure and is not.
    """
    items = snapshot_select.select(iso2, as_of)
    if not items:
        return {"skipped": "no articles in window"}
    for i, item in enumerate(items, start=1):
        item["id"] = f"a{i}"

    scored = rewrite.mask_items(items, iso2)
    scored = digest_engine.digest_articles(
        scored, country_display="a country", iso2=iso2, as_of=as_of,
        masked=True, content_cache=store)
    fulltext_ids = digest_engine.select_fulltext_ids(scored)
    pipeline._rewrite_fulltext(scored, fulltext_ids, iso2)
    degraded = [it.get("id") for it in scored if not isinstance(it.get("digest"), dict)]

    guess = probe.probe(rewrite.mask_items(scored, iso2), api_key,
                        fulltext_ids=fulltext_ids)
    return {"guess": guess, "n": len(items), "degraded": degraded,
            "items": scored, "fulltext_ids": fulltext_ids}


def leaking_text(result: dict, iso2: str) -> list:
    """Exactly the bytes a still-identified bundle sent, so a reader can see why.

    Printed rather than summarised. "Portugal was identified" is a number; what
    fixes layer 2 is the sentence that gave it away.

    Two lessons are baked in here. The scan covers the **full-text block** as
    well as the digests, because that is where a body lands and the first version
    of this function looked only at titles and digests — so it printed "no roster
    term survives" for a bundle the probe had just identified from a president's
    name in a body. And it reports every entry rather than only entries with a
    gazetteer hit: `gazetteer.scan` knows countries, currencies and demonyms, and
    a *person* is none of those. The names that actually leak are invisible to
    it, which is the entire reason the model sweep exists.
    """
    from backend.utils.ai import langchain_llm
    masked = rewrite.mask_items(result["items"], iso2)
    chosen = set(result.get("fulltext_ids") or ())
    roster = list(gazetteer.DEFAULT_ROSTER)
    out = []
    for entry in langchain_llm.prompt_entries(masked):
        blob = " ".join(str(entry.get(f) or "") for f in ("title", "summary"))
        digest = entry.get("digest")
        if isinstance(digest, dict):
            blob += " " + " ".join(str(v) for v in digest.values())
        aid = entry.get("id")
        if aid in chosen:
            body = next((it.get("text") or "" for it in masked
                         if it.get("id") == aid), "")
            blob += " " + body
        out.append({
            "id": aid,
            "in_fulltext": aid in chosen,
            # Gazetteer hits are the ones layer 1 should have caught. Their
            # absence is not a clean bill of health.
            "roster_terms": sorted(set(gazetteer.scan(blob, roster))),
            "text": blob[:400],
        })
    return out


def run(bundles: list, label: str, show_leaks: bool) -> None:
    """Probe every bundle, store each result, and print the diff."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is not set; nothing to probe.")
        return

    baseline = data_push.read_probe_results()
    print(f"\n=== {label}: {len(bundles)} bundle(s) ===")
    print(f"  masking: mask_map={gazetteer.MASK_MAP_VERSION} "
          f"sweep={rewrite.SWEEP_VERSION}  probe model={ai_client.DIGEST_MODEL_NAME}")
    print(f"  baseline rows already stored: {len(baseline)}")

    results, still_named = [], []
    with usage.meter(budget_usd=config.LEAKAGE_SCAN_BUDGET_USD) as meter:
        for iso2, as_of, _ in bundles:
            outcome = probe_one(iso2, as_of, api_key)
            if "skipped" in outcome:
                print(f"  {iso2} {as_of}  skipped: {outcome['skipped']}")
                continue
            guess = outcome["guess"]
            hit = guess.get("country") == iso2
            data_push.upsert_probe_result(
                iso2, as_of, guess,
                mask_map_version=gazetteer.MASK_MAP_VERSION,
                sweep_version=rewrite.SWEEP_VERSION,
                probe_model=ai_client.DIGEST_MODEL_NAME,
                n_articles=outcome["n"])
            results.append({"country_iso2": iso2, "guess": guess})
            print(f"  {iso2} {as_of}  n={outcome['n']:>2}  guess={guess.get('country'):<3} "
                  f"conf={guess.get('confidence'):.2f}  "
                  f"{'IDENTIFIED' if hit else 'not identified'}"
                  f"{'  DEGRADED=' + str(len(outcome['degraded'])) if outcome['degraded'] else ''}")
            print(f"      evidence: {str(guess.get('evidence'))[:220]}")
            if hit:
                still_named.append((iso2, as_of, outcome))

    print(f"\n  metered spend: ${meter.spend_usd:.4f} "
          f"({meter.calls} calls, {meter.input_tokens:,} in / {meter.output_tokens:,} out)")

    summary = probe.summarize([{"country_iso2": r["country_iso2"], "guess": r["guess"]}
                               for r in results])
    print("\n  per-country hit rate:")
    for iso2, stats in sorted(summary["per_country"].items()):
        print(f"    {iso2}  {stats['hits']}/{stats['n']}  rate={stats['rate']:.2f}  "
              f"mean confidence={stats['mean_confidence']:.2f}")
    print(f"    ceiling={summary['ceiling']:.2f}  spread={summary['spread']:.2f}")

    current = data_push.read_probe_results(
        mask_map_version=gazetteer.MASK_MAP_VERSION,
        sweep_version=rewrite.SWEEP_VERSION)
    prior = [r for r in baseline
             if (r["mask_map_version"], r["sweep_version"])
             != (gazetteer.MASK_MAP_VERSION, rewrite.SWEEP_VERSION)]
    if prior:
        print("\n  against the previous masking behaviour:")
        for row in probe.compare(prior, current):
            if row["fixed"] is None and row["was_guess"] is None:
                continue
            print(f"    {row['country_iso2']} {row['as_of']}  "
                  f"was {row['was_guess']}@{row['was_confidence']:.2f} -> "
                  f"now {row['now_guess']}@{row['now_confidence']:.2f}  "
                  f"{'FIXED' if row['fixed'] else 'REGRESSED' if row['regressed'] else '='}")
    else:
        print("\n  no prior masking behaviour stored — this run is the baseline.")

    if show_leaks and still_named:
        print(f"\n=== {len(still_named)} bundle(s) still identified: the text ===")
        for iso2, as_of, outcome in still_named:
            print(f"\n  --- {iso2} {as_of} ---")
            print(f"  probe said: {outcome['guess'].get('evidence')}")
            entries = leaking_text(outcome, iso2)
            named = [e for e in entries if e["roster_terms"]]
            if named:
                print(f"  {len(named)} entry/entries still carry a roster term "
                      f"(a layer-1 miss):")
                for e in named[:4]:
                    print(f"    [{e['id']}] {e['roster_terms']}: {e['text'][:240]}")
            else:
                print("  no roster term survives layer 1 — so whatever gave it "
                      "away is not a country name. The full-text entries, which "
                      "is where a person's name lands:")
            for e in [e for e in entries if e["in_fulltext"]][:3]:
                print(f"    [{e['id']}] FULLTEXT: {e['text'][:400]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recorded", action="store_true",
                        help="the bundles that left a digest-cache trace")
    parser.add_argument("--fresh", action="store_true",
                        help="a reproducible stratified sample across the roster")
    parser.add_argument("--per-country", type=int, default=4)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--no-leaks", action="store_true",
                        help="skip printing the text of identified bundles")
    args = parser.parse_args()

    if args.recorded:
        bundles = recorded_bundles()
        print(f"recorded bundles (from article_digest): {len(bundles)}")
        run(bundles, "recorded bundles — the sweep acceptance test", not args.no_leaks)
    if args.fresh:
        bundles = fresh_bundles(args.per_country, args.seed)
        print(f"\nfresh sample: seed={args.seed}, {args.per_country}/country, "
              f"rule = the {max(1, args.per_country // 2)} largest and "
              f"{max(1, args.per_country // 2)} smallest bundles per country "
              f"over quarterly anchors")
        run(bundles, "fresh sample — the new baseline", not args.no_leaks)
    if not (args.recorded or args.fresh):
        parser.error("pass --recorded, --fresh, or both")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [probe] %(levelname)s %(message)s")
    main()
