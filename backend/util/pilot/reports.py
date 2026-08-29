"""What the pilot measured, rendered for someone to read and argue with.

Five meters, and the ordering is not arbitrary — each one is needed to read the
one before it.

1. **Divergence.** masked − named on the dates both arms scored, split either
   side of the model's knowledge cutoff. This is the headline: what identity was
   worth. It is also the one most easily misread, which is why it is decomposed
   against the third arm rather than reported alone.

   **Signed**, with the magnitude beside it. This was |masked − named|, and an
   absolute value answers "how far apart" while throwing away "which way" — and
   which way is the finding. Masking scoring a country *riskier* than its name
   does means the name was carrying reassurance; scoring it *safer* means the
   name was carrying alarm. Those are opposite defects with opposite fixes, and
   under an absolute value they print as the same number. Worse, a country whose
   weeks split evenly in both directions averages to near zero and reads as
   "masking is clean" when it is nothing of the kind — which is exactly what a
   mean of absolute values was protecting against, so both are reported.

2. **Identifiability.** What the probe guessed. The US is expected near the
   ceiling — a decade of coverage volume gives it away whatever the gazetteer
   does — so the number that matters is the *spread*, not the peak. A low spread
   with a high ceiling means masking is not working; a wide spread means it works
   everywhere except where volume betrays it.

3. **Evidence texture.** Source mix and the full-vs-abstract tier split per
   country per year. A divergence that tracks the abstract share is a statement
   about thin evidence, not about masking.

4. **Spend**, against the governor.

5. **Where identity was carrying a fact the payload does not state.** The step
   that turns the report into work: a country whose divergence survives the
   structural block is a country whose name was doing something the block has
   not replaced.

Everything is read from `history_run_ledger` and `risk_snapshot`. Nothing here
scores, fetches or writes.
"""

import contextlib
import datetime
import io
import json
import logging
import pathlib
import statistics
from typing import Any, Dict, List, Optional, Tuple

from backend.data_upsert import data_push
from backend.data_upsert import store
from backend.util import config
from backend.llm import probe

logger = logging.getLogger(__name__)


def _masked_scores(roster: List[str]) -> Dict[tuple, float]:
    """``{(iso2, as_of): score}`` for the production series.

    From ``risk_snapshot`` because that is where the masked arm writes; the
    ledger holds its manifest and its cost, not its number.
    """
    with data_push._transaction() as cur:
        cur.execute(
            "SELECT country_iso2, as_of, score FROM risk_snapshot "
            "WHERE scoring_mode = 'masked' AND country_iso2 = ANY(%s)", (roster,))
        return {(r[0], r[1]): float(r[2]) for r in cur.fetchall() if r[2] is not None}


def _arm_scores(mode: str) -> Dict[tuple, float]:
    """``{(iso2, as_of): score}`` for a diagnostic arm, out of the ledger."""
    out: Dict[tuple, float] = {}
    for row in store.read_runs(mode):
        result = row.get("result") or {}
        score = result.get("score")
        if score is not None:
            out[(row["country_iso2"], row["as_of"])] = float(score)
    return out


def divergence(roster: Optional[List[str]] = None) -> Dict[str, Any]:
    """What identity was worth, and how much of it the structural block bought back.

    Reported per country and split at ``CUTOFF_DATE``, because a model that can
    simply remember a week is not being tested on the same thing as one that
    cannot.

    The decomposition is the point. masked − named alone cannot distinguish
    "the structural facts recovered what the name carried" from "the name never
    carried anything" — both look like a small number. The no-structural arm
    separates them: if withholding the block widens the gap, the block was
    doing work.

    Every gap is reported twice: signed, so the direction of the failure is
    legible, and absolute, so weeks that diverge in opposite directions cannot
    cancel each other into a clean-looking zero. ``structural_recovery`` reads
    the absolute pair, because "did the block narrow the gap" is a question
    about size and a signed subtraction would answer a different one.
    """
    roster = roster or list(config.PILOT_ROSTER)
    cutoff = datetime.date.fromisoformat(config.CUTOFF_DATE)
    masked = _masked_scores(roster)
    named = _arm_scores("named")
    bare = _arm_scores("masked_nostructural")

    per: Dict[str, Any] = {}
    for iso2 in roster:
        # Signed, masked minus named: positive means masking scored the country
        # riskier than its name did, so the name was carrying reassurance.
        paired = [(day, masked[(iso2, day)] - value)
                  for (c, day), value in named.items()
                  if c == iso2 and (iso2, day) in masked]
        # The same dates again without the structural block, so the two gaps are
        # measured on identical weeks rather than on two different samples.
        paired_bare = [(day, bare[(iso2, day)] - value)
                       for (c, day), value in named.items()
                       if c == iso2 and (iso2, day) in bare]

        pre = [d for day, d in paired if day < cutoff]
        post = [d for day, d in paired if day >= cutoff]
        per[iso2] = {
            "n": len(paired),
            "pre_cutoff": _mean(pre),
            "post_cutoff": _mean(post),
            "overall": _mean([d for _, d in paired]),
            "without_structural": _mean([d for _, d in paired_bare]),
            "n_without_structural": len(paired_bare),
            # The magnitudes, so a country whose weeks diverge in both
            # directions cannot average itself into a clean-looking zero.
            "abs_pre_cutoff": _mean([abs(d) for d in pre]),
            "abs_post_cutoff": _mean([abs(d) for d in post]),
            "abs_overall": _mean([abs(d) for _, d in paired]),
            "abs_without_structural": _mean([abs(d) for _, d in paired_bare]),
        }
        # Positive means the block narrowed the gap: withholding it diverged
        # more. Negative would mean the block is actively misleading the model,
        # which would be worth knowing immediately.
        #
        # Read off the magnitudes, not the signed means. "Did the block narrow
        # the gap" is a question about size, and subtracting two signed means
        # answers a different question — one where a bare arm that diverged
        # hard in the other direction would score as a large recovery.
        overall = per[iso2]["abs_overall"]
        bare_gap = per[iso2]["abs_without_structural"]
        per[iso2]["structural_recovery"] = (
            round(bare_gap - overall, 4) if overall is not None and bare_gap is not None
            else None)
    return per


def _mean(values: List[float]) -> Optional[float]:
    """The mean, or None for an empty sample. Never zero — an unmeasured pair
    and a perfectly agreeing one are different facts."""
    return round(statistics.fmean(values), 4) if values else None


def identifiability(roster: Optional[List[str]] = None) -> Dict[str, Any]:
    """Probe hit rates per country and era, from the manifests of masked runs.

    The probe is stored inside each snapshot's manifest rather than in a table
    of its own, so this reads them back out. A run where the probe did not fire
    — most of them, it samples — simply has no entry.
    """
    roster = roster or list(config.PILOT_ROSTER)
    cutoff = datetime.date.fromisoformat(config.CUTOFF_DATE)
    per: Dict[str, Any] = {}

    for row in store.read_runs("masked"):
        iso2 = row["country_iso2"]
        if iso2 not in roster:
            continue
        guess = ((row.get("manifest") or {}).get("masking") or {}).get("identifiability")
        if not guess:
            continue
        era = "pre_cutoff" if row["as_of"] < cutoff else "post_cutoff"
        stats = per.setdefault(iso2, {"pre_cutoff": [0, 0], "post_cutoff": [0, 0],
                                      "confidence": [], "outcomes": []})
        stats[era][0] += 1
        stats[era][1] += int(guess.get("country") == iso2)
        stats["confidence"].append(float(guess.get("confidence") or 0.0))
        stats["outcomes"].append(probe.classify(iso2, guess))

    out: Dict[str, Any] = {}
    for iso2, stats in per.items():
        out[iso2] = {
            era: {"n": n, "hits": hits, "rate": round(hits / n, 3) if n else None}
            for era, (n, hits) in ((e, stats[e]) for e in ("pre_cutoff", "post_cutoff"))
        }
        out[iso2]["mean_confidence"] = _mean(stats["confidence"])
        total = sum(stats[e][0] for e in ("pre_cutoff", "post_cutoff"))
        hits = sum(stats[e][1] for e in ("pre_cutoff", "post_cutoff"))
        out[iso2]["rate"] = round(hits / total, 3) if total else None
        # Four outcomes, not two. A bundle the probe places confidently in the
        # wrong country is neither a hit nor a clean miss: masking held, and the
        # text was still legible enough to commit to an answer. Counting only
        # hits understates what the evidence carries; counting confidence alone
        # overstates it. PT on a quiet week came back "GB at 0.70".
        counts = {outcome: stats["outcomes"].count(outcome)
                  for outcome in ("identified", "wrong", "uncertain", "no_guess")}
        out[iso2]["outcomes"] = counts
        out[iso2]["placed_rate"] = (
            round((counts["identified"] + counts["wrong"]) / total, 3)
            if total else None)

    rates = [v["rate"] for v in out.values() if v["rate"] is not None]
    return {
        "per_country": out,
        # The meter that actually means something. A high ceiling on its own is
        # expected; a high floor is the failure.
        "ceiling": max(rates) if rates else None,
        "floor": min(rates) if rates else None,
        "spread": round(max(rates) - min(rates), 3) if rates else None,
    }


def evidence_texture(roster: Optional[List[str]] = None) -> Dict[str, Any]:
    """Source mix and the full-vs-abstract split, per country per year.

    Read from the article manifests the masked runs already store, so this
    costs a query rather than a re-assembly of 2,092 snapshots. A divergence
    that tracks the abstract share is a statement about evidence thinness, not
    about masking, and this is how that gets ruled in or out.
    """
    roster = roster or list(config.PILOT_ROSTER)
    per: Dict[tuple, Dict[str, int]] = {}

    for row in store.read_runs("masked"):
        iso2 = row["country_iso2"]
        if iso2 not in roster:
            continue
        articles = (row.get("manifest") or {}).get("articles")
        if not isinstance(articles, list):
            continue  # an empty week, or a manifest that failed to build
        key = (iso2, row["as_of"].year)
        bucket = per.setdefault(key, {"snapshots": 0, "articles": 0, "abstract": 0,
                                      "guardian": 0, "nyt": 0})
        bucket["snapshots"] += 1
        for article in articles:
            bucket["articles"] += 1
            bucket["abstract"] += int(article.get("tier") == "abstract-only")
            # `source`, not `source_system`. `snapshot_select.to_item` sets both
            # to the same string, but the manifest only ever carried the first —
            # so this meter reported guardian=0 nyt=0 abstract=0.000 for every
            # country-year, and read as "the corpus is uniform" rather than as
            # "nothing was measured".
            source = article.get("source")
            if source in bucket:
                bucket[source] += 1

    return {
        f"{iso2} {year}": {
            **counts,
            "abstract_share": (round(counts["abstract"] / counts["articles"], 3)
                               if counts["articles"] else None),
            "articles_per_snapshot": (round(counts["articles"] / counts["snapshots"], 1)
                                      if counts["snapshots"] else None),
        }
        for (iso2, year), counts in sorted(per.items())
    }


def spend() -> Dict[str, Any]:
    """Every dollar the pilot metered, by mode, against the governor."""
    by_mode: Dict[str, Dict[str, Any]] = {}
    for mode in config.SCORING_MODES:
        rows = store.read_runs(mode)
        done = [r for r in rows if r["status"] == "complete"]
        total = sum(r["spend_usd"] or 0.0 for r in rows)
        by_mode[mode] = {
            "runs": len(rows),
            "complete": len(done),
            "failed": sum(1 for r in rows if r["status"] == "failed"),
            "spend_usd": round(total, 2),
            "per_snapshot": round(total / len(done), 4) if done else None,
        }
    grand = store.total_spend_usd()
    return {
        "by_mode": by_mode,
        "total_usd": round(grand, 2),
        "budget_usd": config.PILOT_BUDGET_USD,
        "headroom_usd": round(config.PILOT_BUDGET_USD - grand, 2),
    }


# Sources whose cost does not grow with the roster. The NYT archive endpoint
# returns a whole month of the whole paper in one call and every country is
# filtered out of that same response, so five countries and forty-eight cost
# the same 121 calls. Scaling it would invent work that will never happen.
_ROSTER_WIDE_SOURCES: Tuple[str, ...] = ("nyt",)


def harvest_pacing() -> Dict[str, Any]:
    """How long the corpus took to collect, per source and country.

    Read from what each harvester measured and stamped on its own checkpoint.
    It was briefly inferred from the gap between consecutive ``completed_at``
    stamps, which is exact only while windows run strictly in sequence in one
    uninterrupted process — and the Guardian harvest is neither: it stops on a
    daily quota and resumes eight hours later, so day two's first window would
    have read as an eight-hour window.

    This exists because the 48-country backfill has to be estimated from
    somewhere, and the only honest place is a harvest that actually ran. Five
    countries spanning the US (an order of magnitude more coverage than the
    rest) to PT (thin) is a real sample; a guess is not. Calls are reported
    beside the seconds because the Guardian harvest is quota-bound rather than
    time-bound — the wall arrives at a call count, and hours are what that
    count converts into after the waiting.
    """
    with data_push._transaction() as cur:
        cur.execute("""
            SELECT variant AS source, country_iso2, as_of, status,
                   (detail ->> 'items_written')::int  AS items,
                   (detail ->> 'seconds')::float8     AS seconds,
                   -- float, not int: one NYT archive call serves the whole
                   -- roster and is charged as a fraction to each country it
                   -- covered, so the roster's shares sum back to the one
                   -- request the archive actually saw.
                   (detail ->> 'calls')::float8       AS calls
              FROM run_ledger
             WHERE job_type = 'harvest'
             ORDER BY completed_at
        """)
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    return _pace(rows)


# Three consecutive failed windows on one (source, country). BR ran to eleven
# before anybody noticed, and at three the answer is already the same: this
# country is not being harvested, and no amount of further running will fix it.
STALLED_AFTER_CONSECUTIVE_FAILURES = 3


def _pace(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The arithmetic of :func:`harvest_pacing`, over checkpoint rows. Split out
    so it is testable without a database."""
    per: Dict[tuple, Dict[str, Any]] = {}
    # By window, not by when the row was written. `write_checkpoint` upserts
    # `completed_at = now()` on every retry, so completed-at order is retry
    # order -- a window retried today sorts after one harvested last week, and
    # a run of consecutive failures reads as scattered ones.
    for row in sorted(rows, key=lambda r: (r["source"], r["country_iso2"],
                                           str(r.get("as_of") or ""))):
        key = (row["source"], row["country_iso2"])
        bucket = per.setdefault(key, {"windows": 0, "seconds": 0.0, "items": 0,
                                      "calls": 0, "failed": 0, "untimed": 0,
                                      "streak": 0, "longest_failure_run": 0})
        bucket["windows"] += 1
        # A window whose harvester did not stamp a duration counts toward the
        # corpus and not toward the pacing, and says so. Treating it as zero
        # seconds would make the extrapolation optimistic, which is the
        # direction that costs somebody a day.
        if row.get("seconds") is None:
            bucket["untimed"] += 1
        else:
            bucket["seconds"] += row["seconds"]
        bucket["items"] += row["items"] or 0
        bucket["calls"] += row.get("calls") or 0
        failed = row["status"] != "done"
        bucket["failed"] += int(failed)
        # The signal nobody had. BR's Guardian harvest failed eleven windows in
        # a row and produced no line anybody read; the gap surfaced months later
        # because a purchase decision went looking for it. A total of eleven
        # failures scattered across a hundred windows is a flaky API, and eleven
        # consecutive is a country with no corpus. Only the run tells them apart.
        bucket["streak"] = bucket["streak"] + 1 if failed else 0
        bucket["longest_failure_run"] = max(bucket["longest_failure_run"],
                                            bucket["streak"])

    out = {}
    for (source, iso2), bucket in sorted(per.items()):
        timed = bucket["windows"] - bucket["untimed"]
        bucket.pop("streak")          # running state, not a result
        out[f"{source} {iso2}"] = {
            **bucket,
            # Rounded on the way out: NYT charges a fifth of a call to each of
            # five countries, and binary floating point renders that as
            # 1.5999999999999999 in a report somebody is meant to read.
            "calls": round(bucket["calls"], 2),
            "seconds": round(bucket["seconds"], 1),
            "minutes": round(bucket["seconds"] / 60, 1),
            "seconds_per_window": (round(bucket["seconds"] / timed, 1)
                                   if timed else None),
            "calls_per_window": (round(bucket["calls"] / bucket["windows"], 2)
                                 if bucket["calls"] else None),
        }
    total_seconds = sum(b["seconds"] for b in per.values())
    # The full-backfill number, and both halves of it are easy to get wrong.
    #
    # Only the per-country sources are scaled: a NYT archive call returns the
    # whole paper for every country at once, so its cost is flat in the roster
    # and multiplying it by 48/5 would invent hours of work that never happen.
    #
    # And the divisor counts the countries measured *by those sources*, not
    # every country appearing anywhere in the table. Guardian had harvested one
    # country when NYT had harvested five; dividing Guardian's hour by five and
    # multiplying by 48 priced a 48-country Guardian harvest at ten hours when
    # the same arithmetic on its own sample says forty-eight.
    scaled = sum(b["seconds"] for (source, _), b in per.items()
                 if source not in _ROSTER_WIDE_SOURCES)
    flat = total_seconds - scaled
    scaled_countries = {iso2 for source, iso2 in per
                        if source not in _ROSTER_WIDE_SOURCES}
    return {
        "per_source_country": out,
        # Named, not left to be spotted in a table of a hundred rows. Three in a
        # row is already a country that is not being harvested, and eleven in a
        # row went unread for months because the only place it appeared was a
        # `failed` count that looked like ordinary flakiness.
        "stalled": sorted(
            key for key, b in out.items()
            if b["longest_failure_run"] >= STALLED_AFTER_CONSECUTIVE_FAILURES),
        "total_minutes": round(total_seconds / 60, 1),
        "countries_measured": len({iso2 for _, iso2 in per}),
        "countries_scaled": len(scaled_countries),
        "roster_wide_minutes": round(flat / 60, 1),
        "hours_for_48_countries_linear": (
            round((scaled * (48 / len(scaled_countries)) + flat) / 3600, 1)
            if scaled_countries else None),
    }


def structural_candidates(roster: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Where identity was carrying a fact the payload still does not state.

    Step 11, as a ranked list rather than a paragraph. The question is no longer
    which mode ships — masked ships. It is which countries the structural block
    is failing, and the signal for that is a divergence that *survives* the
    block: if withholding the block barely widens the gap, the block was not
    what was closing it, and something the name implied is still missing.

    Ranked by the divergence the block does not explain. Read the top of this
    list as "these countries' payloads are incomplete", not as "masking failed
    here" — the fix is a new structural field, not a retreat to named scoring.
    """
    ranked = []
    for iso2, row in divergence(roster).items():
        # Ranked on the magnitude: a payload is incomplete by the same amount
        # whether the missing fact made the country look better or worse. The
        # signed mean rides along because the *direction* is what names the
        # missing field — reassurance the block does not state, or alarm it
        # does not state — and that is the next piece of work.
        if row["abs_overall"] is None or not row["n"]:
            continue
        recovery = row["structural_recovery"]
        ranked.append({
            "country": iso2,
            "divergence": row["abs_overall"],
            "signed_divergence": row["overall"],
            # What the block did NOT buy back. When the no-structural arm has
            # not run, this is the whole divergence — unattributed rather than
            # attributed to the block's absence.
            "unexplained": round(row["abs_overall"] - (recovery or 0.0), 4),
            "structural_recovery": recovery,
            "n": row["n"],
            "measured_against_the_third_arm": bool(row["n_without_structural"]),
        })
    return sorted(ranked, key=lambda r: r["unexplained"], reverse=True)


def lint_findings(roster: Optional[List[str]] = None) -> Dict[str, Any]:
    """Advisory tripwires the run recorded, by rule and by country.

    Lint is observe-only by design, and that design was argued for on the basis
    that a contradiction gets written down beside the score and read. `risk_lint`
    had no reader in this codebase, so only the first half was happening — and a
    tripwire nobody reads is not a safety net, it is a table.

    Counted by rule as well as listed, because the two failure modes need
    different answers: one country tripping one rule is a country to look at, and
    a rule firing across the whole roster is a prompt or a threshold to move.
    """
    roster = roster or list(config.PILOT_ROSTER)
    rows = [r for r in data_push.read_lint_findings() if r["country_iso2"] in roster]
    by_rule: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bucket = by_rule.setdefault(row["rule"], {"n": 0, "countries": set()})
        bucket["n"] += 1
        bucket["countries"].add(row["country_iso2"])
    return {
        "total": len(rows),
        "by_rule": {rule: {"n": v["n"], "countries": sorted(v["countries"])}
                    for rule, v in sorted(by_rule.items(), key=lambda kv: -kv[1]["n"])},
        "recent": rows[:10],
    }


def stage1_degradation(roster: Optional[List[str]] = None) -> Dict[str, Any]:
    """Snapshots the scorer read as truncated bodies rather than as digests.

    A stage-1 failure is silent: the article still reaches the model in the
    pre-digest shape, so the snapshot scores fine and says nothing. `28a8889`
    recorded it in the manifest so the two cases would stop being
    indistinguishable; this is what distinguishes them.

    It belongs beside divergence rather than in a footnote. A country whose
    divergence is large *and* whose snapshots were partly degraded is not
    evidence about masking — it is evidence about how much of its evidence
    arrived.
    """
    roster = roster or list(config.PILOT_ROSTER)
    rows = [r for r in data_push.read_stage1_degradation()
            if r["country_iso2"] in roster]
    per: Dict[str, Dict[str, int]] = {}
    for row in rows:
        bucket = per.setdefault(row["country_iso2"], {"snapshots": 0, "articles": 0,
                                                      "degraded": 0, "truncated": 0})
        bucket["snapshots"] += 1
        bucket["articles"] += row["articles"] or 0
        bucket["degraded"] += row["degraded"] or 0
        bucket["truncated"] += row.get("truncated") or 0
    return {
        "affected_snapshots": len(rows),
        "per_country": {
            iso2: {**counts,
                   "degraded_share": (round(counts["degraded"] / counts["articles"], 3)
                                      if counts["articles"] else None)}
            for iso2, counts in sorted(per.items())
        },
    }


def render(roster: Optional[List[str]] = None) -> None:
    """Print all seven meters. The pilot's deliverable."""
    roster = roster or list(config.PILOT_ROSTER)

    print("\n=== 1. divergence: masked - named on paired dates ===")
    print("  (signed, then |.|. Positive = masking scored it riskier than its name")
    print("   did, so the name was carrying reassurance; negative, alarm. `recovery`")
    print("   = how much of the gap the structural block closed, off the magnitudes,")
    print("   from the no-structural arm.)")
    print(f"  {'':<4} {'n':>4} {'pre':>8} {'post':>8} {'overall':>8} "
          f"{'|pre|':>8} {'|post|':>8} {'|overall|':>9} "
          f"{'no-struct':>10} {'|no-str|':>9} {'recovery':>9}")
    for iso2, row in sorted(divergence(roster).items()):
        print(f"  {iso2:<4} {row['n']:>4} {_fmt(row['pre_cutoff']):>8} "
              f"{_fmt(row['post_cutoff']):>8} {_fmt(row['overall']):>8} "
              f"{_fmt(row['abs_pre_cutoff']):>8} {_fmt(row['abs_post_cutoff']):>8} "
              f"{_fmt(row['abs_overall']):>9} "
              f"{_fmt(row['without_structural']):>10} "
              f"{_fmt(row['abs_without_structural']):>9} "
              f"{_fmt(row['structural_recovery']):>9}")

    print("\n=== 2. identifiability: can the cheap model name the country? ===")
    ident = identifiability(roster)
    print("  (the US is expected near the ceiling — a decade of coverage volume")
    print("   gives it away. The spread is the meter; a high floor is the failure.)")
    print("   `wrong` is its own column on purpose: a bundle placed confidently in")
    print("   the wrong country is neither a hit nor a clean miss. Masking held and")
    print("   the text was still legible enough to commit — read `placed` as what")
    print("   the evidence gave away, and `overall` as what identity did.)")
    for iso2, row in sorted(ident["per_country"].items()):
        pre, post = row["pre_cutoff"], row["post_cutoff"]
        counts = row.get("outcomes") or {}
        print(f"  {iso2:<4} overall={_fmt(row['rate']):>6}  "
              f"placed={_fmt(row.get('placed_rate')):>6}  "
              f"pre={_fmt(pre['rate']):>6} (n={pre['n']:>3})  "
              f"post={_fmt(post['rate']):>6} (n={post['n']:>3})  "
              f"confidence={_fmt(row['mean_confidence'])}")
        print(f"       identified={counts.get('identified', 0):<4} "
              f"wrong={counts.get('wrong', 0):<4} "
              f"uncertain={counts.get('uncertain', 0):<4} "
              f"no_guess={counts.get('no_guess', 0)}")
    print(f"  ceiling={_fmt(ident['ceiling'])}  floor={_fmt(ident['floor'])}  "
          f"spread={_fmt(ident['spread'])}")

    print("\n=== 3. evidence texture: source mix and tier split ===")
    for key, row in evidence_texture(roster).items():
        print(f"  {key}  snapshots={row['snapshots']:>3} "
              f"articles/snapshot={_fmt(row['articles_per_snapshot']):>5}  "
              f"abstract={_fmt(row['abstract_share']):>6}  "
              f"guardian={row['guardian']:>5} nyt={row['nyt']:>5}")

    print("\n=== 4. spend ===")
    money = spend()
    for mode, row in money["by_mode"].items():
        print(f"  {mode:<20} {row['complete']:>5} complete, {row['failed']:>3} failed, "
              f"${row['spend_usd']:>7.2f}  (${_fmt(row['per_snapshot'])}/snapshot)")
    print(f"  {'TOTAL':<20} ${money['total_usd']:.2f} of ${money['budget_usd']:.2f} "
          f"— ${money['headroom_usd']:.2f} left")

    print("\n=== 5. ranked: where identity carried a fact the payload does not state ===")
    print("  (divergence the structural block did NOT close. The fix is a new")
    print("   structural field, not a retreat to named scoring.)")
    candidates = structural_candidates(roster)
    if not candidates:
        print("  Nothing to rank — the diagnostic arms have not run.")
    for rank, row in enumerate(candidates, start=1):
        flag = "" if row["measured_against_the_third_arm"] else "  (unattributed: "\
                                                                "no no-structural arm)"
        print(f"  {rank}. {row['country']}  unexplained={_fmt(row['unexplained'])}  "
              f"of |divergence|={_fmt(row['divergence'])}  "
              f"signed={_fmt(row['signed_divergence'])}  n={row['n']}{flag}")

    print("\n=== 6. lint: contradictions the run wrote down ===")
    print("  (advisory — nothing here moved a score. One country on one rule is a")
    print("   country to look at; a rule firing across the roster is a threshold")
    print("   to move or a prompt to fix.)")
    lint = lint_findings(roster)
    if not lint["total"]:
        print("  No findings.")
    for rule, row in lint["by_rule"].items():
        print(f"  {rule:<36} {row['n']:>4}  {', '.join(row['countries'])}")

    print("\n=== 7. stage-1 degradation: snapshots scored on truncated bodies ===")
    print("  (a stage-1 failure is silent — the article still reaches the model in")
    print("   the pre-digest shape. Read this before reading divergence: a country")
    print("   that is both divergent and degraded is telling you about its evidence,")
    print("   not about masking.)")
    degradation = stage1_degradation(roster)
    if not degradation["affected_snapshots"]:
        print("  Every article in every snapshot was digested.")
    for iso2, row in degradation["per_country"].items():
        print(f"  {iso2:<4} {row['snapshots']:>4} snapshot(s) affected, "
              f"{row['degraded']:>4}/{row['articles']:<5} degraded "
              f"({_fmt(row['degraded_share'])}), "
              f"{row.get('truncated', 0):>4} truncated-retry")

    print("\n=== 8. harvest pacing: what the corpus cost in time ===")
    print("  (the input to the 48-country backfill decision. NYT is not scaled —")
    print("   one archive call returns the whole world, so it does not grow with")
    print("   the roster the way Guardian and Wayback do.)")
    pacing = harvest_pacing()
    if not pacing["per_source_country"]:
        print("  Nothing harvested yet.")
    for key, row in pacing["per_source_country"].items():
        notes = "".join([f"  {row['failed']} failed" if row["failed"] else "",
                         f"  {row['untimed']} untimed" if row["untimed"] else "",
                         f"  {row['longest_failure_run']} IN A ROW"
                         if row["longest_failure_run"]
                         >= STALLED_AFTER_CONSECUTIVE_FAILURES else ""])
        print(f"  {key:<14} {row['windows']:>4} window(s)  {row['minutes']:>7.1f} min  "
              f"{row['items']:>7} article(s)  {row['calls']:>7.1f} call(s)  "
              f"{_fmt(row['calls_per_window']):>8}/window{notes}")
    if pacing["countries_measured"]:
        print(f"  total {pacing['total_minutes']:.1f} min; "
              f"{pacing['roster_wide_minutes']:.1f} of it roster-wide (flat in the "
              f"roster size)")
        print(f"  ~{pacing['hours_for_48_countries_linear']}h for 48 countries, "
              f"scaling the per-country sources off "
              f"{pacing['countries_scaled']} measured country/ies")
    if pacing["stalled"]:
        print(f"  STALLED — {STALLED_AFTER_CONSECUTIVE_FAILURES}+ consecutive "
              f"failed windows, so this is not flakiness: "
              f"{', '.join(pacing['stalled'])}")
        print("   (BR ran to eleven in a row before anyone noticed, and the only")
        print("    trace was a `failed` count that read like an unreliable API.)")


def _fmt(value: Optional[float]) -> str:
    """A number, or an em dash. Never 0.0 for absent — an unmeasured pair and a
    perfectly agreeing one must not print the same."""
    return "—" if value is None else f"{value:.3f}"


# --- the exported baseline --------------------------------------------------
# A gate is only a gate if the next run can be compared against it. The first
# gate-2 pass produced its numbers, they were read once, and the artifact was
# never committed — so when the schema rebuild raised "did anything change?",
# the answer had to come from prose in a task brief. The measurement had been
# made and thrown away, which is the same failure as the probe whose result
# survived only in a commit message.
#
# JSON because a diff has to read it, markdown because a person does.

def summary(roster: Optional[List[str]] = None) -> Dict[str, Any]:
    """Every meter in one dict, stamped with the versions that produced it.

    The stamp is the load-bearing part. Two divergence numbers are not
    comparable unless they were measured under the same masking, prompt and
    payload versions, and a baseline that does not say which it used is a
    number without units.
    """
    from backend.util.pilot import score  # local: `run` imports both, order-free

    roster = roster or list(config.PILOT_ROSTER)
    return {
        "captured_under": score.versions(),
        "roster": roster,
        "cutoff_date": config.CUTOFF_DATE,
        "divergence": divergence(roster),
        "identifiability": identifiability(roster),
        "evidence_texture": evidence_texture(roster),
        "spend": spend(),
        "structural_candidates": structural_candidates(roster),
        "lint": lint_findings(roster),
        "stage1_degradation": stage1_degradation(roster),
        "harvest_pacing": harvest_pacing(),
    }


def export(directory: pathlib.Path, roster: Optional[List[str]] = None,
           note: str = "") -> Dict[str, pathlib.Path]:
    """Write the summary as ``GATE2_BASELINE.json`` and ``.md``.

    The markdown embeds `render`'s own output verbatim rather than
    reformatting the same numbers a second way. Two renderers over one dataset
    is two things to keep in agreement, and the one that drifts is always the
    one nobody runs.

    Args:
        directory: where to write. The repo root.
        note: free text for the top of the markdown — what this capture was for.

    Returns:
        ``{"json": path, "markdown": path}``.
    """
    data = summary(roster)
    directory = pathlib.Path(directory)

    json_path = directory / "GATE2_BASELINE.json"
    json_path.write_text(json.dumps(data, indent=2, default=str, sort_keys=True),
                         encoding="utf-8")

    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        render(roster)

    versions = "\n".join(f"| `{field}` | `{value}` |"
                         for field, value in sorted(data["captured_under"].items()))
    md_path = directory / "GATE2_BASELINE.md"
    md_path.write_text(
        f"# Gate-2 baseline\n\n"
        f"{note.strip() + chr(10) + chr(10) if note.strip() else ''}"
        f"What the pilot measured on the anchors gate 2 scores, kept so the next "
        f"run is a regression check rather than a fresh opinion. Regenerate with "
        f"`python -m backend.util.pilot.run pilot-report --export`; the machine-"
        f"readable copy is `GATE2_BASELINE.json` beside this file.\n\n"
        f"## Captured under\n\n"
        f"A divergence measured under different masking is a different number. "
        f"Compare against this baseline only when these match — and when they do "
        f"not, that is the finding.\n\n"
        f"| version | value |\n|---|---|\n{versions}\n\n"
        f"## The meters\n\n```\n{printed.getvalue().strip()}\n```\n",
        encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
