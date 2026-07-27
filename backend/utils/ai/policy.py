"""Risk-scoring policy: the floors, caps and gates applied after the model.

The scorer's job is perception — what the evidence shows. This module's job is
enforcement — what that perception obliges us to score. Splitting them buys
three things the old prompt-embedded rules could not: the rules become
testable, changing a threshold no longer means re-scoring history through the
API, and the model's own judgement survives next to the enforced number
instead of being overwritten by it.

Everything here is **pure**: plain data in, plain data out, no network, no
database, no clock reads. ``as_of`` is always a parameter, never
``date.today()``, which is what makes the whole layer re-runnable over history
for free.

Two kinds of rule live here:

* **Thresholds** — ``risk_policy.yaml``: condition-flag floors, the CPI tiers,
  the political-stability cap. Versioned via ``POLICY_VERSION``.
* **The legal-investability gate** — ``legal_restrictions.yaml``: countries
  under a US sanctions regime that makes securities exposure unlawful are
  forced to 1.0, because "risk" for an investor who legally cannot hold the
  asset is total. This gate used to live in ``langchain_llm``; it moved here
  so all enforcement sits in one place, and it runs *last* because it is
  absolute.

Both scales are 0-1, matching ``risk_snapshot.score``. The model's 0-100
integers are converted back to 0-1 in ``langchain_llm`` before anything here
sees them.
"""

import logging
from datetime import datetime, date
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

import yaml

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).with_name("risk_policy.yaml")
LEGAL_RULES_PATH = Path(__file__).with_name("legal_restrictions.yaml")

# The five sub-factors this module can floor or cap.
_CONFLICT_WAR = "conflict_war"
_POLITICAL_STABILITY = "political_stability"
_MACRO_VOL = "macroeconomic_volatility"


@lru_cache(maxsize=1)
def _load_policy() -> Dict:
    """Load the thresholds once per process.

    Returns:
        The parsed ``risk_policy.yaml``. Empty on any read/parse failure, which
        degrades to "no floors, no caps" — the model's raw scores pass through
        untouched and the failure is logged. The sanctions gate is loaded
        separately and is unaffected.
    """
    try:
        with open(POLICY_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to load risk_policy.yaml: %s", exc)
        return {}


POLICY_VERSION: str = str(_load_policy().get("policy_version") or "unknown")


# -------------------------
# Legal-investability gate (YAML-driven) — moved from langchain_llm
# -------------------------
@lru_cache(maxsize=1)
def _load_legal_rules_index() -> Dict[str, Dict]:
    """Load the sanctions rules once per process, indexed by iso2/code.

    Returns:
        ``{ISO2: entry}``. Empty if the file is unreadable or malformed — the
        failure is logged and the gate simply never fires.
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
    gate, where failing open would understate risk.
    """
    if not s:
        return date.min
    try:
        return datetime.fromisoformat(s[:10]).date()
    except ValueError:
        logger.warning("Unparseable effective_from %r; treating rule as always in force", s)
        return date.min


def _legal_gate_decision(iso2: Optional[str], as_of: date) -> Optional[Dict]:
    """Decide whether the sanctions 1.0 override applies to a country.

    Args:
        iso2: country code to look up, or None (gate never fires).
        as_of: date the score is being computed for; a rule applies only from
            its ``effective_from`` onward.

    Returns:
        ``{name, rule, sources}`` describing the triggering rule, or None when
        the country is unrestricted or the rule is not yet in force.
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


# -------------------------
# Enforcement
# -------------------------
class PolicyResult(NamedTuple):
    """The enforced view of one country's scores.

    ``score_12m``/``score_3m`` are None only when the raw score was None (the
    LLM call failed) — no rule here ever manufactures a number out of nothing.
    """
    score_12m: Optional[float]
    score_3m: Optional[float]
    subscores: Dict[str, Optional[float]]
    applied_rules: List[str]
    gate_note: Optional[str]
    # The triggering rule itself — ``{name, rule, sources}`` — when the gate
    # fired, so a stored 1.0 carries the instrument that forced it and not just
    # the country's name in ``applied_rules``. None when no gate applied.
    legal_gate: Optional[Dict] = None


def _as_float(value) -> Optional[float]:
    """Coerce a raw score to float, or None if it isn't a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _floor(value: Optional[float], floor: float) -> Optional[float]:
    """Raise ``value`` to ``floor``. None stays None — a floor never invents a
    score for something the model did not report."""
    return value if value is None else max(value, floor)


def _flag(condition_flags: Dict, key: str) -> bool:
    """Read a boolean condition flag, treating anything malformed as False."""
    return condition_flags.get(key) is True


def _conflict_level(condition_flags: Dict) -> Optional[str]:
    """Read ``internal_conflict_level``, or None for "none"/missing/garbage."""
    level = condition_flags.get("internal_conflict_level")
    if isinstance(level, str) and level.upper() in ("A", "B", "C"):
        return level.upper()
    if level not in (None, "none", "None", ""):
        logger.debug("Ignoring unrecognized internal_conflict_level %r", level)
    return None


def apply_policy(
    *,
    iso2: Optional[str],
    as_of: date,
    raw_score_12m: Optional[float],
    raw_score_3m: Optional[float],
    raw_subscores: Dict,
    condition_flags: Dict,
    macro_facts: Dict,
) -> PolicyResult:
    """Apply the versioned policy to one country's raw model output.

    Args:
        iso2: country code, for the sanctions lookup. None disables the gate.
        as_of: the date being scored. Never read from the clock — pass the
            snapshot's own date so re-running history gives the same answer.
        raw_score_12m: the model's 12-month score, 0-1, or None if the call
            failed.
        raw_score_3m: the model's 3-month score, 0-1, or None.
        raw_subscores: the model's sub-factor scores, 0-1, values may be None.
        condition_flags: the model's ``condition_flags`` object. Missing keys,
            wrong types, and unknown level strings are treated as inert.
        macro_facts: ``{indicator_pretty_name: latest_value}`` from the macro
            payload — the CPI tiers read the measured number, not the model's
            opinion of it. A missing indicator just means no tier fires.

    Returns:
        A :class:`PolicyResult`, including ``legal_gate`` — the triggering
        sanctions rule when one fired, for the caller to persist. The inputs are
        never mutated: the caller is persisting the raw dicts alongside these
        gated ones.
    """
    # Order matters, and it is:
    #   1. condition-flag floors (war, then internal conflict — highest wins)
    #   2. inflation floors from the measured CPI (first matching tier only)
    #   3. the political-stability cap (lowers; released by rupture flags)
    #   4. the sanctions gate (absolute — overrides everything above)
    # Floors before the cap so a war floor can't be undone by it; the gate last
    # because a legally uninvestable country is 1.0 whatever else is true.
    policy = _load_policy()
    flags = condition_flags if isinstance(condition_flags, dict) else {}
    facts = macro_facts if isinstance(macro_facts, dict) else {}
    applied: List[str] = []

    score_12m = _as_float(raw_score_12m)
    score_3m = _as_float(raw_score_3m)
    # New dict: the caller is persisting raw_subscores unchanged.
    subs: Dict[str, Optional[float]] = {
        k: _as_float(v) for k, v in (raw_subscores or {}).items()
    }

    overall_floor = 0.0
    conflict_floor = 0.0
    macro_vol_floor = 0.0

    # 1) Condition-flag floors. Both horizons: a country in an active war is
    #    not low-risk over three months either.
    if _flag(flags, "war_on_territory"):
        war = policy.get("war_on_territory") or {}
        overall_floor = max(overall_floor, float(war.get("overall_floor") or 0.0))
        conflict_floor = max(conflict_floor, float(war.get("conflict_war_floor") or 0.0))
        applied.append("war_on_territory")

    level = _conflict_level(flags)
    if level:
        rules = (policy.get("internal_conflict") or {}).get(level) or {}
        overall_floor = max(overall_floor, float(rules.get("overall_floor") or 0.0))
        conflict_floor = max(conflict_floor, float(rules.get("conflict_war_floor") or 0.0))
        applied.append(f"internal_conflict_{level}")

    # 2) Inflation floors, from the macro payload. Tiers are ordered high-to-low
    #    in the YAML; the first match wins.
    infl = policy.get("inflation_floors") or {}
    cpi = _as_float(facts.get(infl.get("indicator")))
    if cpi is not None:
        for tier in infl.get("tiers") or []:
            threshold = _as_float(tier.get("at_or_above"))
            if threshold is None or cpi < threshold:
                continue
            overall_floor = max(overall_floor, float(tier.get("overall_floor") or 0.0))
            macro_vol_floor = max(macro_vol_floor, float(tier.get("macro_vol_floor") or 0.0))
            applied.append(f"inflation>={threshold:g}")
            break
    elif infl:
        logger.debug("No %r in macro_facts; inflation floors not evaluated", infl.get("indicator"))

    if overall_floor > 0.0:
        score_12m = _floor(score_12m, overall_floor)
        score_3m = _floor(score_3m, overall_floor)
    if conflict_floor > 0.0:
        subs[_CONFLICT_WAR] = _floor(subs.get(_CONFLICT_WAR), conflict_floor)
    if macro_vol_floor > 0.0:
        subs[_MACRO_VOL] = _floor(subs.get(_MACRO_VOL), macro_vol_floor)

    # 3) Political-stability cap: routine turbulence (caretaker cabinets,
    #    coalition talks, snap elections) stays moderate unless the situation
    #    is a genuine rupture, which is what the releasing flags mark.
    cap_rule = policy.get("political_stability_cap") or {}
    cap = _as_float(cap_rule.get("value"))
    if cap is not None:
        released = any(_flag(flags, k) for k in (cap_rule.get("released_by") or []))
        current = subs.get(_POLITICAL_STABILITY)
        if not released and current is not None and current > cap:
            subs[_POLITICAL_STABILITY] = cap
            applied.append("political_stability_cap")

    # 4) The sanctions gate, last and absolute.
    gate_note = None
    gate = _legal_gate_decision(iso2, as_of)
    if gate:
        logger.info("Legal-investability gate triggered (override): %s", gate["name"])
        gate_note = (
            f"Legal-investability gate triggered for {gate['name']}: "
            f"{gate['rule']} ⇒ score forced to 1.0."
        )
        applied.append(f"sanctions_gate:{gate['name']}")
        # A failed call stays failed: the caller skips a None-scored country,
        # and resurrecting it here would write a score with no assessment.
        score_12m = None if score_12m is None else 1.0
        score_3m = None if score_3m is None else 1.0

    return PolicyResult(
        score_12m=score_12m,
        score_3m=score_3m,
        subscores=subs,
        applied_rules=applied,
        gate_note=gate_note,
        legal_gate=gate,
    )
