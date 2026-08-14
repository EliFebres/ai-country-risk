"""Advisory tripwires: contradictions worth noticing, never worth correcting.

When the enforcement layer was deleted, the question it had been answering badly
did not go away: what happens when the model flags an active war and then scores
the country a 44? The old answer was to overwrite the 44. This module's answer
is to write both down, next to each other, and let a human look.

That is the whole design. Every rule here is an **observation about a
disagreement**, not a correction:

* Nothing here returns a score, and nothing that calls it may change one.
* Nothing here blocks. A lint failure is logged and the pipeline continues; the
  snapshot writes either way.
* The thresholds below are tripwires chosen to fire rarely enough to be worth
  reading, not policy. They encode no view about what the score *should* be. If
  a rule fires constantly, the honest fixes are to improve the prompt or move
  the tripwire — not to start clamping scores again.

Findings land in the log and in the ``risk_lint`` table, keyed
(country, as_of, rule), so a re-run of the same day replaces its own findings
rather than accumulating duplicates.
"""

import logging
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- Advisory tripwires -----------------------------------------------------
# Scores here are on the model's 0-100 integer scale, matching what the prompt
# asks for and what lint receives before the 0-1 conversion is read back out.
#
# These are deliberately loose. A war flag beside a score of 68 is a judgement
# call worth a second look; beside a 44 it is a contradiction. The gap between
# those two numbers is where the model is allowed to disagree with the naive
# reading, which is the entire reason enforcement was removed.

# An active war on the country's own territory alongside a 12-month score below
# this is hard to justify from any evidence base.
WAR_SCORE_FLOOR = 70

# Bank runs, capital controls or missed payments are a 3-month problem by
# definition; a low near-term score beside that flag is a contradiction.
SOVEREIGN_STRESS_SCORE_FLOOR = 55

# A currency held quiet under a managed regime while reserves drain is supposed
# to *raise* order-uncertainty. A low reading beside a true flag means the model
# took manufactured calm at face value, which the prompt tells it not to.
SUPPRESSED_CALM_UNCERTAINTY_FLOOR = 40


def _as_int(value: Any) -> Optional[int]:
    """Coerce a score to int, or None if it is not a usable number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flag(flags: Dict, key: str) -> bool:
    """Read a boolean condition flag, treating anything malformed as False."""
    return isinstance(flags, dict) and flags.get(key) is True


def check(
    *,
    country_iso2: str,
    as_of: date,
    condition_flags: Optional[Dict] = None,
    score_3m: Optional[int] = None,
    score_12m: Optional[int] = None,
    ledger_scores: Optional[Dict] = None,
    suppressed_vol_flag: Optional[bool] = None,
    non_investable: bool = False,
) -> List[Dict[str, Any]]:
    """Find contradictions between what the model flagged and what it scored.

    Pure: no I/O, no clock. Every input is passed in, so the same inputs always
    produce the same findings and the rules are testable without a database.

    Args:
        country_iso2: the country these findings belong to.
        as_of: the snapshot date, half of the finding's key.
        condition_flags: the model's ``condition_flags`` object.
        score_3m: the model's 3-month score, 0-100.
        score_12m: the model's 12-month score, 0-100.
        ledger_scores: the model's four ledger scores, 0-100.
        suppressed_vol_flag: the payload's computed flag, or None if any of its
            inputs was unavailable. None is not False and fires nothing.
        non_investable: whether the sanctions badge applied.

    Returns:
        A list of ``{country_iso2, as_of, rule, detail}`` findings, empty when
        nothing tripped. ``detail`` carries the numbers that produced the
        finding so the log line is self-contained.
    """
    flags = condition_flags if isinstance(condition_flags, dict) else {}
    ledgers = ledger_scores if isinstance(ledger_scores, dict) else {}
    findings: List[Dict[str, Any]] = []

    def add(rule: str, detail: Dict[str, Any]) -> None:
        findings.append({
            "country_iso2": country_iso2,
            "as_of": as_of,
            "rule": rule,
            "detail": detail,
        })

    # Both flag/score contradictions share the `flag_score_divergence` rule
    # name, and `risk_lint` is keyed (country, as_of, rule) — so they are
    # collected into ONE finding carrying a list, rather than emitted as two
    # rows that would collide on the primary key. A country can be at war and in
    # sovereign stress at once, which makes that collision routine rather than
    # theoretical.
    divergences: List[Dict[str, Any]] = []

    twelve = _as_int(score_12m)
    if _flag(flags, "war_on_territory") and twelve is not None and twelve < WAR_SCORE_FLOOR:
        divergences.append({
            "flag": "war_on_territory",
            # Named `observed_score`, not `score`: this is a note about a
            # number, and a lint detail must never be mistakable for one.
            "observed_score": twelve,
            "horizon": "12m",
            "tripwire": WAR_SCORE_FLOOR,
            "note": "war flagged on own territory beside a low 12-month score",
        })

    three = _as_int(score_3m)
    if _flag(flags, "sovereign_stress") and three is not None and three < SOVEREIGN_STRESS_SCORE_FLOOR:
        divergences.append({
            "flag": "sovereign_stress",
            "observed_score": three,
            "horizon": "3m",
            "tripwire": SOVEREIGN_STRESS_SCORE_FLOOR,
            "note": "sovereign stress flagged beside a low 3-month score",
        })

    if divergences:
        add("flag_score_divergence", {"divergences": divergences})

    order = _as_int(ledgers.get("order_uncertainty"))
    if (suppressed_vol_flag is True and order is not None
            and order < SUPPRESSED_CALM_UNCERTAINTY_FLOOR):
        add("calm_taken_at_face_value", {
            "order_uncertainty": order,
            "tripwire": SUPPRESSED_CALM_UNCERTAINTY_FLOOR,
            "note": "suppressed volatility detected but order-uncertainty scored low",
        })

    if non_investable:
        # Not a contradiction — an audit trail. Every badged country-day gets a
        # row, so the badge's history is reconstructable without diffing YAML.
        add("non_investable", {
            "score_12m": twelve,
            "note": "sanctions badge applied; score is the model's own and was not adjusted",
        })

    return findings


def log_findings(findings: List[Dict[str, Any]]) -> None:
    """Write findings to the log at a level matching what they mean."""
    for finding in findings:
        # The badge entry is bookkeeping; the rest are disagreements.
        level = logger.info if finding["rule"] == "non_investable" else logger.warning
        level("[lint] %s %s: %s", finding["country_iso2"], finding["rule"], finding["detail"])
