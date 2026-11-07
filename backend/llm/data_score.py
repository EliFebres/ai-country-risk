"""
The data half of the score: every scored indicator on a fixed absolute scale.

A value like inflation needs no model judgement. It becomes a number by where
it sits on a scale with two fixed ends, written in the registry: the value at
which the risk reading is 0 and the value at which it is 100, linear between
and clamped outside. A value maps to the same score whatever the rest of the
roster looks like, so a country's score does not move when another country's
data changes, and there is no peer group to choose.

Step 5b ranked values as percentiles across the roster, and that failed face
validity: small gaps in a tight distribution became large score gaps, and
series that describe an economy's shape (unemployment, Gini, growth) were
ranked as if they were risk. Those series are now `scored: False` and the
registry's `risk_scale` says what the ends are.

A missing indicator is left out of the ledger mean and counted. It is never
filled in: an absent number is absent, not average.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.util import constants

__all__ = ["scale_value", "score_indicator", "ledger_data_scores", "SCORED_CODES"]

SCORED_CODES = tuple(
    code for code, spec in constants.INDICATOR_REGISTRY.items() if spec.get("scored")
)

for _code in SCORED_CODES:
    _scale = constants.INDICATOR_REGISTRY[_code].get("risk_scale") or {}
    if "zero_at" not in _scale or "hundred_at" not in _scale or _scale["zero_at"] == _scale["hundred_at"]:
        raise RuntimeError(
            f"INDICATOR_REGISTRY[{_code!r}] is scored but has no usable risk_scale"
        )

_TRANSFORMS = {
    None: lambda x: x,
    "abs_from_2": lambda x: abs(x - 2.0),
}


def scale_value(code: str, value: float) -> Dict[str, Any]:
    """Map one raw value onto its registry scale. Returns value, transformed value, score."""
    scale = constants.INDICATOR_REGISTRY[code]["risk_scale"]
    x = _TRANSFORMS[scale.get("transform")](float(value))
    zero, hundred = float(scale["zero_at"]), float(scale["hundred_at"])
    raw = 100.0 * (x - zero) / (hundred - zero)
    return {
        "value": float(value),
        "scaled_input": round(x, 4),
        "score": round(max(0.0, min(100.0, raw)), 2),
        "clamped": raw < 0 or raw > 100,
    }


def score_indicator(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Score one resolved economics-block entry, or None if it is not scored."""
    code = entry.get("code")
    spec = constants.INDICATOR_REGISTRY.get(code)
    if not spec or not spec.get("scored") or entry.get("value") is None:
        return None
    return {
        "code": code,
        "ledger": spec["ledger"],
        "period": entry.get("period"),
        **scale_value(code, entry["value"]),
    }


def ledger_data_scores(ledgers: Dict[str, Any]) -> Dict[str, Any]:
    """The data score for each ledger from a payload's economics block.

    Args:
        ledgers: ``economics["ledgers"]``: ``{ledger: {"indicators": [{"code",
            "value", "period", ...}]}}``.

    Returns:
        ``{"indicators": {code: {...}}, "data_scores": {ledger: float|None},
        "n_by_ledger": {ledger: int}, "unscored": [codes present but not
        scored]}``. The ledger score is the mean of its scored indicators, or
        None when none resolved.
    """
    scored: Dict[str, Dict[str, Any]] = {}
    unscored: List[str] = []
    data_scores: Dict[str, Optional[float]] = {}
    n_by_ledger: Dict[str, int] = {}

    for ledger in constants.LEDGERS:
        block = ledgers.get(ledger) or {}
        vals: List[float] = []
        for entry in block.get("indicators") or []:
            got = score_indicator(entry)
            if got is None:
                if entry.get("code") in constants.INDICATOR_REGISTRY:
                    unscored.append(entry["code"])
                continue
            scored[got["code"]] = got
            vals.append(got["score"])
        n_by_ledger[ledger] = len(vals)
        data_scores[ledger] = round(sum(vals) / len(vals), 2) if vals else None

    return {
        "indicators": scored,
        "data_scores": data_scores,
        "n_by_ledger": n_by_ledger,
        "unscored": unscored,
    }
