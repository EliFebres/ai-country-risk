"""
Check the answer against the whole schema, before anything rescales it.

A grammar enforces structure, not bounds. Strict schema mode guarantees the
shape — the keys are there and the types are right — and says nothing about
whether an integer declared 0–100 came back as 140, or whether the article ids
correspond to articles that were actually sent.

So the decoded answer is checked here, **before any rescale or clamp**, and
every violation is counted into the census. A run where the model returned three
out-of-range scores and a clamp quietly fixed them is a different run from one
where it did not, and the stored row should be able to tell them apart.

**Every clamp records the raw value it clamped.** A number that was silently
corrected is a number nobody can audit, and "the score was 84" reads identically
whether the model said 84 or said 140.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from backend.llm import constants as ai_constants

__all__ = ["validate", "ValidationResult"]


class ValidationResult(dict):
    """The checked answer, plus what was wrong with it.

    A dict so it can go straight into JSONB, with the fields named rather than
    positional.
    """

    @property
    def violations(self) -> List[Dict[str, Any]]:
        return self["violations"]

    @property
    def ok(self) -> bool:
        return not self["violations"]


def _clamp(
    value: Any,
    field: str,
    violations: List[Dict[str, Any]],
    *,
    low: int = 0,
    high: int = 100,
    allow_null: bool = False,
) -> Optional[int]:
    """Return `value` inside [low, high], recording the raw value if it moved."""
    if value is None:
        if not allow_null:
            violations.append({"field": field, "problem": "missing", "raw": None})
        return None
    try:
        as_int = int(round(float(value)))
    except (TypeError, ValueError):
        violations.append({"field": field, "problem": "not a number", "raw": value})
        return None
    if as_int < low or as_int > high:
        violations.append({
            "field": field,
            "problem": "out of bounds",
            "raw": value,
            "clamped_to": max(low, min(high, as_int)),
        })
        return max(low, min(high, as_int))
    return as_int


def validate(
    answer: Any,
    *,
    article_ids: Sequence[str] = (),
) -> ValidationResult:
    """Validate a decoded scoring answer against the full contract.

    Args:
        answer: What the model returned, already JSON-decoded.
        article_ids: The ids that were actually sent, so an answer about an
            article that was never supplied can be caught.

    Returns:
        A :class:`ValidationResult` carrying the cleaned answer under
        ``"answer"`` and every problem under ``"violations"``.
    """
    violations: List[Dict[str, Any]] = []

    if not isinstance(answer, dict):
        return ValidationResult({
            "answer": None,
            "violations": [{"field": "<root>", "problem": "not an object",
                            "raw": type(answer).__name__}],
        })

    cleaned: Dict[str, Any] = {}

    for field in ("score_12m", "score_3m"):
        cleaned[field] = _clamp(answer.get(field), field, violations)

    for field in ai_constants.LEDGER_FIELDS:
        # Ledger scores may legitimately be null when a ledger had nothing to
        # read. That is a statement about the evidence, and it must survive
        # rather than being defaulted to a number.
        cleaned[field] = _clamp(
            answer.get(field), field, violations, allow_null=True
        )

    flags = answer.get("condition_flags")
    if not isinstance(flags, dict):
        violations.append({"field": "condition_flags", "problem": "missing",
                           "raw": flags})
        cleaned["condition_flags"] = {}
    else:
        clean_flags = {}
        for name in ai_constants.CONDITION_FLAGS:
            value = flags.get(name)
            if not isinstance(value, bool):
                violations.append({
                    "field": f"condition_flags.{name}",
                    "problem": "not a boolean", "raw": value,
                })
                value = bool(value)
            clean_flags[name] = value
        for name in flags:
            if name not in ai_constants.CONDITION_FLAGS:
                violations.append({"field": f"condition_flags.{name}",
                                   "problem": "unknown flag", "raw": flags[name]})
        cleaned["condition_flags"] = clean_flags

    summary = answer.get("bullet_summary")
    if not isinstance(summary, str) or not summary.strip():
        violations.append({"field": "bullet_summary", "problem": "missing",
                           "raw": summary})
        summary = ""
    cleaned["bullet_summary"] = summary.strip()

    evidence = answer.get("subscore_evidence")
    if not isinstance(evidence, dict):
        violations.append({"field": "subscore_evidence", "problem": "missing",
                           "raw": evidence})
        evidence = {}
    clean_evidence = {}
    for name in ai_constants.LEDGER_FIELDS:
        text = evidence.get(name)
        if not isinstance(text, str) or not text.strip():
            violations.append({"field": f"subscore_evidence.{name}",
                               "problem": "missing", "raw": text})
            text = ""
        clean_evidence[name] = text.strip()
    cleaned["subscore_evidence"] = clean_evidence

    scores = answer.get("article_scores")
    if not isinstance(scores, list):
        violations.append({"field": "article_scores", "problem": "missing",
                           "raw": scores})
        scores = []
    known = set(article_ids)
    seen = set()
    clean_scores = []
    for i, row in enumerate(scores):
        if not isinstance(row, dict):
            violations.append({"field": f"article_scores[{i}]",
                               "problem": "not an object", "raw": row})
            continue
        aid = row.get("id")
        if known and aid not in known:
            # An answer about an article that was never sent is the model
            # answering a question nobody asked.
            violations.append({"field": f"article_scores[{i}].id",
                               "problem": "unknown article", "raw": aid})
            continue
        if aid in seen:
            violations.append({"field": f"article_scores[{i}].id",
                               "problem": "duplicate article", "raw": aid})
            continue
        seen.add(aid)
        clean_scores.append({
            "id": aid,
            "door": row.get("door"),
            "bearing": _clamp(row.get("bearing"), f"article_scores[{i}].bearing",
                              violations, allow_null=True),
            "note": (row.get("note") or "").strip(),
        })
    cleaned["article_scores"] = clean_scores

    for missing_id in known - seen:
        # Silent, and the reason articles eleven to twenty used to enter Top-3
        # selection with an impact of zero: the model was never asked about them
        # and nothing noticed.
        violations.append({"field": "article_scores", "problem": "article not scored",
                           "raw": missing_id})

    return ValidationResult({"answer": cleaned, "violations": violations})
