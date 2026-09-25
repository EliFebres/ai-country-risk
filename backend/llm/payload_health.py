"""
What the registry promised against what actually reached the model.

This codebase's recurring failure has not been code that crashed. It has been
code that ran, wrote something plausible, and had no consumer — so every count
looked right and nobody ever read the number. The census is the countermeasure,
and it only works if it is stored and compared.

Three things live here, and keeping them apart is the point:

**The census is state.** Per country per run: indicators expected against
resolved by ledger and by source, every dropped one named with its reason, the
article funnel with per-theme counts, every rejected article with the label and
the reason it was rejected for, the body-status mix, digests generated against
cached, and the versions of everything that could have changed the answer. Taiwan's zeros appear in every single run's table, because state that is
always the same is still information.

**The alarm is change**, and it lives in `quality_report` rather than here. It
compares a run against the country's own recent median, not against an absolute
floor: Taiwan resolving zero is expected because Taiwan always resolved zero,
while Portugal going from twenty indicators to twelve is a source break. It runs
at the end of the run so the flags read as one block instead of scrolling past
one country at a time.

**`evidence_coverage` is computed, never authored.** Asked to self-report it, a
model returned 80 with twenty articles and 80 with six. It is arithmetic over
the census — article volume against budget, how much of each article was read,
indicator resolution per ledger, and freshness — and the components are stored
beside the value so the number can be argued with.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Sequence

from backend.util import constants
from backend.util.hashing import content_hash

__all__ = [
    "COVERAGE_WEIGHTS",
    "BODY_DEPTH",
    "build_census",
    "evidence_coverage",
    "payload_fingerprint",
    "format_census",
]


# How much of an article the model actually got. A headline is not the
# reporting, and coverage should not pretend otherwise.
BODY_DEPTH = {
    "full": 1.0,
    "clipped": 0.85,
    "digest-only": 0.5,
    "title-only": 0.1,
}

# Indicator resolution carries the most weight because it is the half of the
# payload that does not depend on a given week being newsworthy. Freshness is
# smallest because an annual series is *supposed* to be old.
COVERAGE_WEIGHTS = {
    "article_volume": 0.30,
    "body_depth": 0.20,
    "indicator_resolution": 0.35,
    "freshness": 0.15,
}

# A run whose per-ledger resolution falls this far below the country's own
# recent median is a source break rather than a quiet week.
ALARM_DROP = 0.5


def payload_fingerprint(
    economics: Dict[str, Any], selected: Sequence[Dict[str, Any]]
) -> str:
    """Hash the evidence, so two runs can be compared for having read the same thing.

    Covers the resolved indicator codes with their vintages and the ordered
    selected article URLs. Two runs with the same fingerprint read the same
    evidence — which is the only way to tell a score that moved because the
    country moved from one that moved because the model did.
    """
    parts: List[str] = []
    for ledger in constants.LEDGERS:
        for ind in economics["ledgers"][ledger]["indicators"]:
            parts.append(f"{ind['code']}@{ind['period']}~{ind['as_of']}")
    parts.append("|")
    for a in selected:
        parts.append(a.get("publisher_link") or a.get("link") or "")
    return content_hash("\n".join(parts))


def evidence_coverage(
    economics: Dict[str, Any],
    selected: Sequence[Dict[str, Any]],
    *,
    budget: int,
) -> Dict[str, Any]:
    """Compute coverage from the census. Returns the value and its components."""
    res = economics["resolution"]

    volume = min(len(selected) / budget, 1.0) if budget else 0.0

    if selected:
        depth = sum(
            BODY_DEPTH.get(a.get("body_status", "title-only"), 0.1) for a in selected
        ) / len(selected)
    else:
        depth = 0.0

    # Averaged per ledger rather than over all indicators, so one empty ledger
    # costs a quarter of this component however many the others resolved. A
    # payload can be rich overall and blind in one quarter of the framework.
    per_ledger = []
    for ledger in constants.LEDGERS:
        expected = res["expected_by_ledger"].get(ledger, 0)
        resolved = res["resolved_by_ledger"].get(ledger, 0)
        per_ledger.append((resolved / expected) if expected else 0.0)
    indicator_resolution = sum(per_ledger) / len(per_ledger) if per_ledger else 0.0

    resolved_all = [
        ind
        for ledger in constants.LEDGERS
        for ind in economics["ledgers"][ledger]["indicators"]
    ]
    if resolved_all:
        fresh = sum(1 for i in resolved_all if not i["stale_for_its_cadence"])
        freshness = fresh / len(resolved_all)
    else:
        freshness = 0.0

    components = {
        "article_volume": round(volume, 4),
        "body_depth": round(depth, 4),
        "indicator_resolution": round(indicator_resolution, 4),
        "freshness": round(freshness, 4),
    }
    value = round(
        100 * sum(components[k] * w for k, w in COVERAGE_WEIGHTS.items())
    )
    return {
        "evidence_coverage": int(value),
        "components": components,
        "weights": dict(COVERAGE_WEIGHTS),
    }


def build_census(
    iso2: str,
    as_of: dt.date,
    *,
    economics: Dict[str, Any],
    pool_report: Dict[str, Any],
    gate: Dict[str, Any],
    digests: Dict[str, int],
    versions: Dict[str, Any],
    budget: int,
    schema_violations: int = 0,
) -> Dict[str, Any]:
    """Assemble one country's census row for this run."""
    selected = gate["selected"]
    res = economics["resolution"]

    status_mix: Dict[str, int] = {}
    quality_mix: Dict[str, int] = {}
    for a in selected:
        key = a.get("body_status", "title-only")
        status_mix[key] = status_mix.get(key, 0) + 1
        # What the digest said each body is. `partial` and `not_article` are
        # the walls it caught; `unassessed` is a body no digest vouched for.
        if (a.get("text") or "").strip():
            q = a.get("body_quality") or "unassessed"
            quality_mix[q] = quality_mix.get(q, 0) + 1

    coverage = evidence_coverage(economics, selected, budget=budget)

    return {
        "country_iso2": iso2,
        "as_of": as_of,
        "payload_fingerprint": payload_fingerprint(economics, selected),
        "evidence_coverage": coverage["evidence_coverage"],
        "coverage_components": {
            "components": coverage["components"],
            "weights": coverage["weights"],
        },
        "indicators": {
            "expected_by_ledger": res["expected_by_ledger"],
            "resolved_by_ledger": res["resolved_by_ledger"],
            "empty_ledgers": res["empty_ledgers"],
            "by_source": res["by_source"],
            "dropped": res["dropped"],
            "as_of_schemes": res["as_of_schemes"],
        },
        "articles": {
            "fetched": pool_report.get("fetched", 0),
            "stale_republications": pool_report.get("stale_republications", 0),
            "after_dedupe": pool_report.get("after_dedupe", 0),
            "per_theme_fetched": pool_report.get("per_theme", {}),
            "passed_gate": gate["counts"]["eligible"],
            "selected": gate["counts"]["selected"],
            "budget": budget,
            "rejected_by_label": gate["counts"]["rejected_by_label"],
            # The rejections themselves, not just their counts. The labels also
            # live in `llm_artifact`, but only under a hash nobody can join on
            # by eye — and "was the gate right?" is a question somebody has to
            # be able to ask a week later without recomputing a cache key.
            "rejected": gate.get("rejected", []),
            # What the gate said about EVERY candidate, not just the ones it
            # turned away, and why. Without it, asking "what did the classifier
            # decide about this article" means recomputing a content hash by
            # hand — which misses on any article whose body was not stored
            # byte-for-byte as the gate read it — and drawing a stratified
            # sample of its judgements is impossible.
            "gate_labels": gate.get("gate_labels", []),
            "per_theme_selected": gate["per_theme"],
            "per_ledger_selected": gate["per_ledger"],
            "body_status": status_mix,
            "body_quality": quality_mix,
            "digests_generated": digests.get("generated", 0),
            "digests_cached": digests.get("cached", 0),
            "digests_clipped": status_mix.get("clipped", 0),
            "digests_truncated_retry": digests.get("truncated_retry", 0),
            "digests_failed": digests.get("failed", 0),
            "duplicate_slots": pool_report.get("duplicate_slots", 0),
            "query_name": pool_report.get("query_name"),
        },
        "versions": {**versions, "schema_violations": schema_violations},
    }


def format_census(census: Dict[str, Any]) -> str:
    """Render the census as the lines a run prints."""
    ind = census["indicators"]
    art = census["articles"]
    lines = [
        f"[census] {census['country_iso2']} {census['as_of']}  "
        f"coverage={census['evidence_coverage']}  "
        f"fingerprint={(census['payload_fingerprint'] or '')[:12]}",
        f"[census]   indicators resolved {ind['resolved_by_ledger']} "
        f"of {ind['expected_by_ledger']}  dates={ind['as_of_schemes']}",
        f"[census]   articles fetched={art['fetched']} deduped={art['after_dedupe']} "
        f"passed={art['passed_gate']} selected={art['selected']}/{art['budget']} "
        f"rejected={art['rejected_by_label']}",
        f"[census]   bodies {art['body_status']}  quality {art.get('body_quality', {})}  digests "
        f"{art['digests_generated']} new / {art['digests_cached']} cached / "
        f"{art['digests_truncated_retry']} truncated-retry",
        f"[census]   per-theme selected {art['per_theme_selected']}  "
        f"per-ledger {art['per_ledger_selected']}",
    ]
    if ind["dropped"]:
        lines.append(
            "[census]   dropped: "
            + ", ".join(f"{d['code']} ({d['reason']})" for d in ind["dropped"])
        )
    if ind["empty_ledgers"]:
        lines.append(
            f"[census]   LEDGER WITH NO INDICATORS: {', '.join(ind['empty_ledgers'])}"
        )
    return "\n".join(lines)
