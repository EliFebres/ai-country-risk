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

# The acceptance set: the six historical bundles that had left a digest-cache
# trace as of 2026-08-12, which is every trace the 2026-08-03 probe run left
# behind. Pinned rather than queried, and that is the correction of a real bug
# in this script.
#
# It used to derive the set from `article_digest` — a table this harness itself
# writes. So probing a fresh sample added those bundles to "recorded", and the
# set grew from six to fourteen between two runs an hour apart. An acceptance
# test whose composition changes every time it runs is not measuring a change in
# masking, it is measuring which bundles happened to be in the table, and the
# two are indistinguishable in the output.
#
# Add to this list deliberately, in a commit, or not at all.
RECORDED_BUNDLES = (
    ("US", datetime.date(2017, 3, 11)),
    ("US", datetime.date(2018, 5, 12)),
    ("TR", datetime.date(2018, 8, 18)),
    ("US", datetime.date(2019, 7, 13)),
    ("US", datetime.date(2020, 11, 7)),
    ("TR", datetime.date(2021, 3, 27)),
    # The three the 2026-08-13 fresh sample identified, added deliberately per
    # the rule above. An acceptance set of bundles that all passed cannot show a
    # masking change working; these are the only three that failed, so they are
    # the only three that can.
    #
    # TR 2017-01-02 (the failed coup) and KR 2018-07-02 (the denuclearization
    # summit) are ceilings rather than gaps: an event that only happened in one
    # country identifies it however well the names are masked, and no rule fixes
    # that. They are here to be watched, not fixed.
    ("TR", datetime.date(2017, 1, 2)),
    ("KR", datetime.date(2018, 7, 2)),
    # BR 2018-07-02 is the exception and the reason this list grew. It is a gap,
    # not a ceiling — the probe named a footballer as its evidence, and rule 2
    # mapped named people to their office while an athlete has not got one. HEAD
    # closed that. BR left the roster, so `--fresh` will never draw this bundle
    # again; its articles are still in the store, and this is the only direct
    # test that the fix works. Kept out of the roster baseline in the report for
    # exactly that reason: it is a regression test, not a sample.
    ("BR", datetime.date(2018, 7, 2)),
)

# The one bundle in `RECORDED_BUNDLES` whose country is no longer in the roster.
# Reported separately so a country nobody is scoring cannot move the baseline.
OFF_ROSTER_BUNDLES = (("BR", datetime.date(2018, 7, 2)),)


def recorded_bundles(off_roster: bool = False) -> list:
    """The pinned acceptance set, with each bundle's current article count.

    Split on roster membership. The off-roster half is one bundle — BR's World
    Cup week, the only direct test of the rule HEAD added — and folding it into
    the hit rate would let a country nobody is scoring move the baseline.
    """
    out = []
    for iso2, as_of in RECORDED_BUNDLES:
        if ((iso2, as_of) in OFF_ROSTER_BUNDLES) != off_roster:
            continue
        start, end = snapshot_select.window(as_of)
        out.append((iso2, as_of, len(store.read_window(iso2, start, end))))
    return out


def fresh_bundles(per_country: int) -> list:
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

    There is no seed, and there used to be one printed in the report header. It
    was never read — the draw is a total order over bundle size and date, with
    nothing random in it. A parameter that does nothing is worse than no
    parameter, because the report attributed its reproducibility to the seed and
    the first person to change it would have concluded the sample was fixed. The
    rule above is what makes this redrawable, so the rule is what gets printed.

    A consequence of the rule worth stating: it yields ``2 * (per_country // 2)``
    bundles per country, so the count is always even. Twenty bundles cannot be
    drawn from a four-country roster. The stored "fresh 20" was five countries at
    four each, before BR left.
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


def _alternatives(guess: dict) -> str:
    """The ranked top-3 as one line, or a note that the probe offered none."""
    alts = guess.get("alternatives") or []
    return ", ".join(f"{a.get('country')}={float(a.get('probability') or 0):.2f}"
                     for a in alts) or "none offered"


def run(bundles: list, label: str, show_leaks: bool) -> None:
    """Probe every bundle, store each result, and print the diff."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is not set; nothing to probe.")
        return

    baseline = data_push.read_probe_results()
    print(f"\n=== {label}: {len(bundles)} bundle(s) ===")
    print(f"  masking: mask_map={gazetteer.MASK_MAP_VERSION} "
          f"sweep={rewrite.SWEEP_VERSION}")
    print(f"  probe  : model={ai_client.DIGEST_MODEL_NAME} "
          f"version={probe.PROBE_VERSION}")
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
                probe_version=probe.PROBE_VERSION,
                n_articles=outcome["n"])
            results.append({"country_iso2": iso2, "guess": guess})
            print(f"  {iso2} {as_of}  n={outcome['n']:>2}  guess={guess.get('country'):<3} "
                  f"conf={guess.get('confidence'):.2f}  "
                  f"{'IDENTIFIED' if hit else 'not identified'}"
                  f"{'  DEGRADED=' + str(len(outcome['degraded'])) if outcome['degraded'] else ''}")
            # The top-3, not just the argmax. A bundle the probe ranks the truth
            # second at 0.45 is not masked, and every line above this one reports
            # it as a clean miss.
            print(f"      top-3   : {_alternatives(guess)}"
                  f"{'  (insufficient_information)' if guess.get('insufficient_information') else ''}")
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

    # The prior, made visible. A country named far more often than it appears is
    # the model's base rate leaking into the meter, and the hit rates above are
    # that plus whatever the corpus gave away.
    dist = probe.distribution(results)
    print("\n  guess distribution vs. what was actually probed:")
    print(f"    guessed : {dist['guessed']}")
    print(f"    truth   : {dist['truth']}")
    over = {k: v for k, v in dist["over_representation"].items() if abs(v) >= 0.05}
    print(f"    over-represented (>=0.05): {over or 'none'}")
    print(f"    said insufficient_information: "
          f"{dist['insufficient_information']}/{dist['n']}")
    unsure = [r for r in results if r["guess"].get("insufficient_information")]
    hits_when_unsure = sum(1 for r in unsure
                           if r["guess"].get("country") == r["country_iso2"])
    if unsure:
        print(f"    of those, still correct: {hits_when_unsure}/{len(unsure)} "
              f"— correct-by-prior, not by evidence")

    current = data_push.read_probe_results(
        mask_map_version=gazetteer.MASK_MAP_VERSION,
        sweep_version=rewrite.SWEEP_VERSION)
    prior = [r for r in baseline
             if (r["mask_map_version"], r["sweep_version"])
             != (gazetteer.MASK_MAP_VERSION, rewrite.SWEEP_VERSION)]
    if prior:
        print("\n  against the previous masking behaviour:")
        # What the diff may be attributed to, stated rather than assumed.
        #
        # An identical probe is necessary and not sufficient, and assuming it was
        # sufficient is how this line was wrong the first time it was written.
        # The probe reads a *bundle*, and the bundle is produced by the gazetteer,
        # the sweep and the full-text rewrite together. Any of them moving changes
        # what is being measured. The rewrite in particular changes the *volume*
        # of text rather than its content: when its token budget was a digest cap,
        # most bodies degraded to titles, and a bundle missing its bodies probes
        # cleaner for a reason that has nothing to do with masking.
        #
        # So all three versions are compared, and they are named. A run that moves
        # more than one of them measures their sum, and when two of them push in
        # opposite directions — stricter rules, more text — the sum can be zero
        # while neither component is.
        prior_probes = sorted({(r["probe_model"], r["probe_version"]) for r in prior})
        now_probe = (ai_client.DIGEST_MODEL_NAME, probe.PROBE_VERSION)
        moved = []
        if prior_probes != [now_probe]:
            moved.append(f"the probe itself ({prior_probes} -> {now_probe})")
        prior_sweeps = sorted({r["sweep_version"] for r in prior})
        moved.append(f"the mask rules (sweep {'/'.join(prior_sweeps)} -> "
                     f"{rewrite.SWEEP_VERSION})")
        moved.append(f"the full-text rewrite (version {rewrite.REWRITE_VERSION}, "
                     f"token budget now sized from the body — bodies that used "
                     f"to degrade to title-only now reach the probe whole)")
        if prior_probes == [now_probe]:
            print(f"    the probe is unchanged: {now_probe[1]} on {now_probe[0]}, "
                  f"both sides. That rules the instrument out and nothing else.")
        for item in moved:
            print(f"    MOVED: {item}")
        print("    The diff below is the sum of the above. A bundle that reads")
        print("    REGRESSED may simply have got its body back; one that reads")
        print("    FIXED may have lost it. Attribution needs one mover at a time.")
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


def run_control(repeats: int, size: int) -> None:
    """Probe bundles with no country in them, and print what it says anyway.

    The deflator for every other number here. A probe that must name a country
    names the one its prior favours, so "US identified at 0.85" and "the model
    always says US" are the same output — and only this tells them apart. If the
    control names a country at high confidence, every identifiability rate in
    this report is that baseline plus whatever the corpus actually leaked, and
    the baseline has to come off before the rest means anything.

    Cheap by construction: the null bundles are synthetic, so nothing is
    digested and the only calls are the probes themselves.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is not set; nothing to probe.")
        return

    print(f"\n=== control arm: {repeats} null bundle(s) of {size} articles ===")
    print("  synthetic evidence with the shape of a real snapshot and no country")
    print("  in it — `gazetteer.scan` finds nothing, and neither should the probe.")
    results = []
    with usage.meter(budget_usd=config.LEAKAGE_SCAN_BUDGET_USD) as meter:
        for i in range(repeats):
            bundle = probe.null_bundle(size)
            guess = probe.probe(bundle, api_key)
            results.append({"country_iso2": "ZZ", "guess": guess})
            print(f"  [{i + 1}] guess={guess.get('country'):<3} "
                  f"conf={guess.get('confidence'):.2f}  "
                  f"insufficient={str(guess.get('insufficient_information')):<5} "
                  f"[{_alternatives(guess)}]")
            print(f"      {str(guess.get('evidence'))[:200]}")

    print(f"\n  metered spend: ${meter.spend_usd:.4f} ({meter.calls} calls)")
    named = [r for r in results
             if r["guess"].get("country") not in ("ZZ", "")
             and not r["guess"].get("insufficient_information")]
    dist = probe.distribution(results)
    print(f"\n  guessed: {dist['guessed']}")
    print(f"  said insufficient_information: {dist['insufficient_information']}"
          f"/{dist['n']}")
    print(f"  named a country anyway, without flagging it a guess: {len(named)}"
          f"/{dist['n']}")
    if named:
        mean_conf = statistics.fmean(r["guess"]["confidence"] for r in named)
        print(f"  mean confidence when it did: {mean_conf:.2f}")
        print("\n  READ THIS BEFORE THE RATES ABOVE: the probe names countries")
        print("  from bundles containing none, so its identifiability numbers")
        print("  carry that prior and need deflating by it.")
    else:
        print("\n  the probe declines to name a country with nothing to go on, so")
        print("  its identifications elsewhere are about the evidence.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recorded", action="store_true",
                        help="the bundles that left a digest-cache trace")
    parser.add_argument("--control", type=int, metavar="N", default=0,
                        help="probe N null bundles to measure the model's prior")
    parser.add_argument("--control-size", type=int, default=20,
                        help="articles per null bundle; match a real snapshot")
    parser.add_argument("--fresh", action="store_true",
                        help="a reproducible stratified sample across the roster")
    parser.add_argument("--per-country", type=int, default=4)
    parser.add_argument("--no-leaks", action="store_true",
                        help="skip printing the text of identified bundles")
    args = parser.parse_args()

    # The control runs first when asked for, because it is the number every
    # other number in the report has to be read against.
    if args.control:
        run_control(args.control, args.control_size)
    if args.recorded:
        bundles = recorded_bundles()
        print(f"recorded bundles (pinned in this file): {len(bundles)}")
        run(bundles, "recorded bundles — the sweep acceptance test", not args.no_leaks)
        off = recorded_bundles(off_roster=True)
        if off:
            run(off, "off-roster regression test — reported apart from the "
                     "baseline, because a country nobody scores must not move it",
                not args.no_leaks)
    if args.fresh:
        bundles = fresh_bundles(args.per_country)
        half = max(1, args.per_country // 2)
        # The rule, not a seed. This is the whole of what makes the sample
        # redrawable, and it is what a later run has to match to be comparable.
        print(f"\nfresh sample: {len(bundles)} bundle(s), "
              f"{2 * half}/country over {len(config.PILOT_ROSTER)} countries, "
              f"rule = the {half} largest and {half} smallest bundles per country "
              f"over quarterly anchors, ties broken on the anchor date. No seed: "
              f"nothing in the draw is random.")
        run(bundles, "fresh sample — the new baseline", not args.no_leaks)
    if not (args.recorded or args.fresh or args.control):
        parser.error("pass --control, --recorded, --fresh, or any combination")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [probe] %(levelname)s %(message)s")
    main()
