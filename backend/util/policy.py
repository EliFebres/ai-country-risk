"""Legal investability: which countries an investor may not lawfully hold.

This module used to be the enforcement layer — condition-flag floors, CPI tiers,
a political-stability cap, and a sanctions gate that forced a score to 1.0. All
of that is gone as of ``POLICY_VERSION`` below. What remains is a lookup, and
the distinction matters:

* A **floor** was a claim about risk, expressed by overwriting the model's
  judgement with a number from a YAML file. Nobody downstream could tell which
  half of a stored score came from the model and which from the rule, which made
  both unauditable.
* A **badge** is a claim about law. Whether US persons may lawfully hold a
  country's securities is a fact about the sanctions regime, not an opinion
  about that country's risk, and it belongs next to the score rather than inside
  it.

So the gate no longer touches any score. It yields ``non_investable`` plus the
triggering rule, both persisted alongside the model's own numbers, and the
front-end renders a RESTRICTED badge. A sanctioned country keeps whatever score
the evidence earned it — which is also the only way its score series stays
readable across the date a sanctions regime starts or ends.

Contradictions between what the model flagged and what it scored are now
recorded by ``util.lint`` as advisory observations. Nothing corrects them.

Everything here is **pure**: plain data in, plain data out, no network, no
database, no clock reads. ``as_of`` is always a parameter, never
``date.today()``, which is what makes the layer re-runnable over history for
free — a backfill of an old date gets that date's sanctions rules, not today's.
"""

import logging
from datetime import datetime, date
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

import yaml

logger = logging.getLogger(__name__)

LEGAL_RULES_PATH = Path(__file__).with_name("legal_restrictions.yaml")

# Bumped when the *meaning* of a stored score changes, so a time series can be
# split on it. p1.0 (2026-07-26) stored a score that had been through floors,
# caps and a sanctions override. p2.0 (2026-07-27) stores the model's own
# score_12m untouched: enforcement was deleted, sanctions became a badge, and
# risk_policy.yaml was removed with the rules it configured. A p1.0 row and a
# p2.0 row are not comparable for a sanctioned country, and are only loosely
# comparable for any country a floor once bound.
POLICY_VERSION: str = "p2.0-observe-only"


@lru_cache(maxsize=1)
def _load_legal_rules_index() -> Dict[str, Dict]:
    """Load the sanctions rules once per process, indexed by iso2/code.

    Returns:
        ``{ISO2: entry}``. Empty if the file is unreadable or malformed — the
        failure is logged and the badge simply never applies.
    """
    try:
        with open(LEGAL_RULES_PATH, "r", encoding="utf-8") as f:
            y = yaml.safe_load(f) or {}
        entries = y.get("entries") or []
        idx: Dict[str, Dict] = {}
        for e in entries:
            key = (e.get("iso2") or e.get("code") or "").upper()
            if key:
                idx[key] = e
        return idx
    except Exception as exc:
        logger.warning("Failed to load legal_restrictions.yaml: %s", exc)
        return {}


def _parse_iso_date(s: Optional[str]) -> date:
    """Parse a YAML ``effective_from`` date.

    Returns ``date.min`` for missing or unparseable values so a rule with a bad
    date is treated as already in force — the safe direction for a sanctions
    badge, where failing open would understate a legal restriction.
    """
    if not s:
        return date.min
    try:
        return datetime.fromisoformat(s[:10]).date()
    except ValueError:
        logger.warning("Unparseable effective_from %r; treating rule as always in force", s)
        return date.min


def _legal_gate_decision(iso2: Optional[str], as_of: date) -> Optional[Dict]:
    """Decide whether a country is legally uninvestable on a given date.

    Args:
        iso2: country code to look up, or None (never applies).
        as_of: date the score is being computed for; a rule applies only from
            its ``effective_from`` onward.

    Returns:
        ``{name, rule, sources}`` describing the triggering rule, or None when
        the country is unrestricted or the rule is not yet in force. The name
        ``_legal_gate_decision`` is kept from when this drove a gate; it now
        drives a badge, and nothing it returns is applied to a score.
    """
    if not iso2:
        return None
    rules = _load_legal_rules_index()
    entry = rules.get(iso2.upper())
    if not entry:
        return None

    trigger = (entry.get("trigger") or {}).get("set_score_1_0") is True
    if not trigger:
        return None

    eff = _parse_iso_date(entry.get("effective_from"))
    if as_of >= eff:
        return {
            "name": entry.get("name") or iso2,
            "rule": entry.get("rule") or "Sanctions investability prohibition",
            "sources": entry.get("sources") or []
        }
    return None


class InvestabilityResult(NamedTuple):
    """What the legal lookup found. No scores in, no scores out.

    Attributes:
        non_investable: True when a sanctions regime makes securities exposure
            unlawful on ``as_of``.
        legal_gate: the triggering rule — ``{name, rule, sources}`` — so a badged
            row carries the instrument behind it and not just a boolean. None
            when nothing applied.
        note: a sentence for the summary, or None. The badge lives in the
            sidebar, but the map tooltip and the right rail show only the score
            and the summary, so the fact would otherwise disappear on two of the
            three surfaces where a country is read.
        applied_rules: observations for the provenance column. Never enforcement.
    """
    non_investable: bool
    legal_gate: Optional[Dict]
    note: Optional[str]
    applied_rules: List[str]


def assess_investability(*, iso2: Optional[str], as_of: date) -> InvestabilityResult:
    """Look up whether a country is legally investable on ``as_of``.

    Args:
        iso2: country code. None disables the lookup.
        as_of: the date being scored. Never read from the clock — pass the
            snapshot's own date so re-running history gives that date's answer.

    Returns:
        An :class:`InvestabilityResult`. Note what is absent from it: any score.
        This function cannot change one.
    """
    gate = _legal_gate_decision(iso2, as_of)
    if not gate:
        return InvestabilityResult(
            non_investable=False, legal_gate=None, note=None, applied_rules=[],
        )

    logger.info("Legally non-investable (badge, score untouched): %s", gate["name"])
    return InvestabilityResult(
        non_investable=True,
        legal_gate=gate,
        note=(
            f"Legally non-investable for US persons as of {as_of.isoformat()}: "
            f"{gate['rule']} The risk score below is the analyst assessment and is "
            f"not adjusted for this restriction."
        ),
        applied_rules=[f"sanctions_badge:{gate['name']}"],
    )
