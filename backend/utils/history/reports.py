"""What the pilot measured, rendered for someone to read and argue with.

Five meters, and the ordering is not arbitrary — each one is needed to read the
one before it.

1. **Divergence.** |masked - named| on the dates both arms scored, split either
   side of the model's knowledge cutoff. This is the headline: what identity was
   worth. It is also the one most easily misread, which is why it is decomposed
   against the third arm rather than reported alone.

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

import datetime
import logging
import statistics
from typing import Any, Dict, List, Optional

from backend.utils.data_upsert import data_push
from backend.utils.history import config, store

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

    The decomposition is the point. |masked - named| alone cannot distinguish
    "the structural facts recovered what the name carried" from "the name never
    carried anything" — both look like a small number. The no-structural arm
    separates them: if withholding the block widens the gap, the block was
    doing work.
    """
    roster = roster or list(config.PILOT_ROSTER)
    cutoff = datetime.date.fromisoformat(config.CUTOFF_DATE)
    masked = _masked_scores(roster)
    named = _arm_scores("named")
    bare = _arm_scores("masked_nostructural")

    per: Dict[str, Any] = {}
    for iso2 in roster:
        paired = [(day, abs(masked[(iso2, day)] - value))
                  for (c, day), value in named.items()
                  if c == iso2 and (iso2, day) in masked]
        # The same dates again without the structural block, so the two gaps are
        # measured on identical weeks rather than on two different samples.
        paired_bare = [(day, abs(bare[(iso2, day)] - value))
                       for (c, day), value in named.items()
                       if c == iso2 and (iso2, day) in bare]

        per[iso2] = {
            "n": len(paired),
            "pre_cutoff": _mean([d for day, d in paired if day < cutoff]),
            "post_cutoff": _mean([d for day, d in paired if day >= cutoff]),
            "overall": _mean([d for _, d in paired]),
            "without_structural": _mean([d for _, d in paired_bare]),
            "n_without_structural": len(paired_bare),
        }
        # Positive means the block narrowed the gap: withholding it diverged
        # more. Negative would mean the block is actively misleading the model,
        # which would be worth knowing immediately.
        overall, bare_gap = per[iso2]["overall"], per[iso2]["without_structural"]
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
                                      "confidence": []})
        stats[era][0] += 1
        stats[era][1] += int(guess.get("country") == iso2)
        stats["confidence"].append(float(guess.get("confidence") or 0.0))

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
    costs a query rather than a re-assembly of 2,610 snapshots. A divergence
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
            source = article.get("source_system")
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
        if row["overall"] is None or not row["n"]:
            continue
        recovery = row["structural_recovery"]
        ranked.append({
            "country": iso2,
            "divergence": row["overall"],
            # What the block did NOT buy back. When the no-structural arm has
            # not run, this is the whole divergence — unattributed rather than
            # attributed to the block's absence.
            "unexplained": round(row["overall"] - (recovery or 0.0), 4),
            "structural_recovery": recovery,
            "n": row["n"],
            "measured_against_the_third_arm": bool(row["n_without_structural"]),
        })
    return sorted(ranked, key=lambda r: r["unexplained"], reverse=True)


def render(roster: Optional[List[str]] = None) -> None:
    """Print all five meters. The pilot's deliverable."""
    roster = roster or list(config.PILOT_ROSTER)

    print("\n=== 1. divergence: |masked - named| on paired dates ===")
    print("  (higher = identity was worth more. `recovery` = how much of the gap")
    print("   the structural block closed, from the no-structural arm.)")
    print(f"  {'':<4} {'n':>4} {'pre':>8} {'post':>8} {'overall':>8} "
          f"{'no-struct':>10} {'recovery':>9}")
    for iso2, row in sorted(divergence(roster).items()):
        print(f"  {iso2:<4} {row['n']:>4} {_fmt(row['pre_cutoff']):>8} "
              f"{_fmt(row['post_cutoff']):>8} {_fmt(row['overall']):>8} "
              f"{_fmt(row['without_structural']):>10} "
              f"{_fmt(row['structural_recovery']):>9}")

    print("\n=== 2. identifiability: can the cheap model name the country? ===")
    ident = identifiability(roster)
    print("  (the US is expected near the ceiling — a decade of coverage volume")
    print("   gives it away. The spread is the meter; a high floor is the failure.)")
    for iso2, row in sorted(ident["per_country"].items()):
        pre, post = row["pre_cutoff"], row["post_cutoff"]
        print(f"  {iso2:<4} overall={_fmt(row['rate']):>6}  "
              f"pre={_fmt(pre['rate']):>6} (n={pre['n']:>3})  "
              f"post={_fmt(post['rate']):>6} (n={post['n']:>3})  "
              f"confidence={_fmt(row['mean_confidence'])}")
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
              f"of divergence={_fmt(row['divergence'])}  n={row['n']}{flag}")


def _fmt(value: Optional[float]) -> str:
    """A number, or an em dash. Never 0.0 for absent — an unmeasured pair and a
    perfectly agreeing one must not print the same."""
    return "—" if value is None else f"{value:.3f}"
