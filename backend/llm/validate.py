"""
Check the checklist answer against the whole contract, before any arithmetic.

A grammar enforces structure, not meaning. Strict schema mode guarantees the
shape — every question is there, each level is an integer 0–4 or null — and
says nothing about whether the evidence cited was ever sent, whether a level
has any evidence at all, or whether a scheduled event's date is a date.

So the decoded answer is checked here, **before `combine` reads it**, and every
correction is recorded and counted. A level that lost its evidence and became
null is a different reading from a level the model left null, and the stored
row should be able to tell them apart.

The rules the code enforces, which the prompt does not:

- Evidence is **article ids only**. The prompt also allows "indicator names",
  but the economics are scored in code from the same numbers, so a level that
  only restated an indicator would count it twice. Those citations are dropped
  and counted separately (`evidence_dropped_non_article`), so how often the
  model reaches for them is a measurement.
- A non-null level with no surviving evidence becomes null. The prompt says a
  level must cite at least one; the grammar cannot say so.
- A scheduled event counts only with a strict ISO date inside the horizon, an
  article id that was sent and a known question.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence

from backend.llm import constants as ai_constants
from backend.util import constants as util_constants

__all__ = ["validate", "ValidationResult", "COUNT_KEYS"]

COUNT_KEYS = (
    "levels_nulled_no_evidence",
    "levels_out_of_range",
    "evidence_dropped",
    "evidence_dropped_non_article",
    "events_dropped",
)

#: Registry codes and labels, lower-cased, so an indicator cited as evidence can
#: be told from an invented article id when it is counted.
_INDICATOR_NAMES = {
    s.lower()
    for code, spec in util_constants.INDICATOR_REGISTRY.items()
    for s in (code, spec["label"], spec["label"].split(" (")[0])
}


class ValidationResult(dict):
    """The checked answer, plus what was wrong with it.

    A dict so it can go straight into JSONB, with the fields named rather than
    positional.
    """

    @property
    def violations(self) -> List[Dict[str, Any]]:
        return self["violations"]

    @property
    def counts(self) -> Dict[str, int]:
        return self["counts"]

    @property
    def ok(self) -> bool:
        return not self["violations"]


def _looks_like_indicator(item: str) -> bool:
    text = item.strip().lower()
    if text in _INDICATOR_NAMES:
        return True
    # "Inflation (% y/y) 8.7%" or "FP.CPI.TOTL.ZG: 8.72" still name the indicator.
    return any(name in text for name in _INDICATOR_NAMES if len(name) > 5)


def _parse_date(raw: Any) -> Optional[dt.date]:
    if not isinstance(raw, str) or len(raw.strip()) != 10:
        return None
    try:
        return dt.date.fromisoformat(raw.strip())
    except ValueError:
        return None


def validate(
    answer: Any,
    *,
    article_ids: Sequence[str] = (),
    run_date: Optional[dt.date] = None,
) -> ValidationResult:
    """Validate a decoded checklist answer against the full contract.

    Args:
        answer: What the model returned, already JSON-decoded.
        article_ids: The ids that were actually sent, so a citation of an
            article that was never supplied can be dropped.
        run_date: The date the evidence is read as of; the scheduled-event
            horizon is measured from it. Defaults to today.

    Returns:
        A :class:`ValidationResult` carrying the cleaned answer under
        ``"answer"``, every problem under ``"violations"`` and the tallies
        under ``"counts"``.
    """
    violations: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {k: 0 for k in COUNT_KEYS}
    run_date = run_date or dt.date.today()

    if not isinstance(answer, dict):
        return ValidationResult({
            "answer": None,
            "violations": [{"field": "<root>", "problem": "not an object",
                            "raw": type(answer).__name__}],
            "counts": counts,
        })

    known = set(article_ids)
    level_max = ai_constants.SCORING["level_max"]
    cleaned: Dict[str, Any] = {"questions": {}}

    for qid in ai_constants.QUESTION_IDS:
        row = answer.get(qid)
        if not isinstance(row, dict):
            violations.append({"field": qid, "problem": "missing", "raw": row})
            cleaned["questions"][qid] = {"level": None, "evidence": [], "note": ""}
            continue

        level = row.get("level")
        if level is not None:
            try:
                as_int = int(round(float(level)))
            except (TypeError, ValueError):
                violations.append({"field": f"{qid}.level", "problem": "not a number",
                                   "raw": level})
                as_int = None
            if as_int is not None and not (0 <= as_int <= level_max):
                violations.append({"field": f"{qid}.level", "problem": "out of range",
                                   "raw": level})
                counts["levels_out_of_range"] += 1
                as_int = None
            level = as_int

        raw_evidence = row.get("evidence")
        if not isinstance(raw_evidence, list):
            violations.append({"field": f"{qid}.evidence", "problem": "not a list",
                               "raw": raw_evidence})
            raw_evidence = []
        evidence: List[str] = []
        for item in raw_evidence:
            text = item.strip() if isinstance(item, str) else item
            if text in known and text not in evidence:
                evidence.append(text)
            elif text in evidence:
                continue
            elif isinstance(text, str) and _looks_like_indicator(text):
                counts["evidence_dropped_non_article"] += 1
                violations.append({"field": f"{qid}.evidence", "problem": "indicator cited",
                                   "raw": item})
            else:
                counts["evidence_dropped"] += 1
                violations.append({"field": f"{qid}.evidence", "problem": "unknown article",
                                   "raw": item})

        if level is not None and not evidence:
            # The prompt says a non-null level must cite at least one article.
            # A level with nothing behind it is the model's reputation of the
            # country, which is what the prompt forbids.
            violations.append({"field": f"{qid}.level", "problem": "no evidence",
                               "raw": level})
            counts["levels_nulled_no_evidence"] += 1
            level = None

        cleaned["questions"][qid] = {
            "level": level,
            "evidence": evidence,
            "note": (row.get("note") or "").strip() if isinstance(row.get("note"), str) else "",
        }

    # Scheduled events: each one must be datable, inside the horizon, about an
    # article that was sent and a question that exists. Anything else is dropped
    # and named, because a dropped event is three points of score_3m.
    horizon = dt.timedelta(days=ai_constants.SCORING["event_horizon_days"])
    raw_events = answer.get("scheduled_events")
    if not isinstance(raw_events, list):
        violations.append({"field": "scheduled_events", "problem": "not a list",
                           "raw": raw_events})
        raw_events = []
    events: List[Dict[str, Any]] = []
    seen_events = set()
    for i, ev in enumerate(raw_events):
        field = f"scheduled_events[{i}]"
        if not isinstance(ev, dict):
            violations.append({"field": field, "problem": "not an object", "raw": ev})
            counts["events_dropped"] += 1
            continue
        problem = None
        when = _parse_date(ev.get("date"))
        aid = ev.get("article_id")
        qid = ev.get("question")
        direction = ev.get("direction")
        if when is None:
            problem = "unparseable date"
        elif when <= run_date:
            problem = "past"
        elif when > run_date + horizon:
            problem = "beyond horizon"
        elif known and aid not in known:
            problem = "unknown article"
        elif qid not in ai_constants.QUESTION_IDS:
            problem = "unknown question"
        elif direction not in ai_constants.EVENT_DIRECTIONS:
            problem = "unknown direction"
        elif (aid, qid, when) in seen_events:
            problem = "duplicate"
        if problem:
            violations.append({"field": field, "problem": problem, "raw": ev})
            counts["events_dropped"] += 1
            continue
        seen_events.add((aid, qid, when))
        events.append({
            "article_id": aid,
            "date": when.isoformat(),
            "event": (ev.get("event") or "").strip(),
            "question": qid,
            "direction": direction,
        })
    cleaned["scheduled_events"] = events

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

    return ValidationResult({"answer": cleaned, "violations": violations, "counts": counts})
