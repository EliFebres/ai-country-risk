"""
The outlook's contract, enforced after the grammar.

The strict schema guarantees shape: every field present, every enum legal.
It cannot say that a range contains its midpoint's band, that a cited article
was in the payload, that a date is inside the horizon, or that the prose quotes
the range the structured fields committed to. Those are checked here, the way
`validate.py` checks the scorer: the answer is cleaned, every repair is a
counted violation, and the row is written with the cleaned answer plus the
violation list in its manifest. Nothing is silently fixed.

Kept apart from `validate.py` because that module is 470 lines of scorer
contract (quotes, headline-only evidence, routine deadlines) and none of it
applies here.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Optional, Sequence

import backend.llm.constants as ai_constants
from backend.util import gazetteer

__all__ = ["validate_outlook", "COUNT_KEYS", "band_of", "range_in_text"]

S = ai_constants.OUTLOOK_SCORING

COUNT_KEYS = (
    "range_out_of_range", "range_swapped", "p_band_renormalised",
    "p_band_midpoint_not_modal", "ledger_direction_invalid",
    "drivers_truncated", "drivers_dropped", "driver_ledger_corrected",
    "driver_evidence_dropped", "uncertainties_truncated", "confidence_clamped",
    "prose_clipped", "headed_clipped", "headed_em_dash", "headed_range_missing",
    "headed_direction_missing", "watch_resolution_unknown_item",
    "watch_resolution_duplicate", "watch_unaddressed", "watch_evidence_dropped",
    "new_watch_truncated", "new_watch_dropped", "new_watch_kind_corrected",
    "new_watch_evidence_dropped", "mask_leak_in_output",
)

_RANGE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:-|–|—|to)\s*(\d{1,3})(?!\d)")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def band_of(score: float) -> str:
    """Which of the three bands a score_12m falls in."""
    for name, (lo, hi) in S["bands"].items():
        if lo <= score <= hi:
            return name
    return "high" if score > 100 else "low"


def range_in_text(text: str) -> List[tuple]:
    """Every `NN-NN` or `NN to NN` pair quoted in `text`."""
    return [(int(a), int(b)) for a, b in _RANGE.findall(text or "")]


def _clip(text: str, limit: int) -> str:
    """Cut at the last sentence end before `limit`, or hard at `limit`."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind(".\n"), head.rfind("."))
    return head[: cut + 1].rstrip() if cut > limit // 2 else head.rstrip()


def _parse_date(s: Any) -> Optional[dt.date]:
    if not isinstance(s, str) or not _ISO.match(s):
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def validate_outlook(
    parsed: Any,
    *,
    run_date: dt.date,
    open_item_ids: Sequence[str] = (),
    article_ids: Sequence[str] = (),
    iso2: Optional[str] = None,
) -> Dict[str, Any]:
    """Clean one decoded outlook answer against the contract.

    Returns:
        ``{"answer": cleaned, "violations": [{field, problem, raw}], "counts": {...}}``.
        `answer` is None when `parsed` is not an object.
    """
    violations: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {k: 0 for k in COUNT_KEYS}

    def flag(field: str, problem: str, raw: Any = None, count: Optional[str] = None) -> None:
        violations.append({"field": field, "problem": problem, "raw": raw})
        if count:
            counts[count] += 1

    if not isinstance(parsed, dict):
        flag("<root>", "answer is not an object", type(parsed).__name__)
        return {"answer": None, "violations": violations, "counts": counts}

    known_ids = set(article_ids)
    open_ids = set(open_item_ids)
    out: Dict[str, Any] = {}

    # --- horizons -----------------------------------------------------------
    for key in ("horizon_4w", "horizon_12w"):
        h = dict(parsed.get(key) or {})
        lo, hi = h.get("score_low"), h.get("score_high")
        try:
            lo, hi = int(lo), int(hi)
        except (TypeError, ValueError):
            flag(f"{key}.range", "not integers", (lo, hi), "range_out_of_range")
            lo, hi = 0, 100
        for name, v in (("score_low", lo), ("score_high", hi)):
            if not 0 <= v <= 100:
                flag(f"{key}.{name}", "outside 0..100", v, "range_out_of_range")
        lo, hi = max(0, min(100, lo)), max(0, min(100, hi))
        if lo > hi:
            flag(f"{key}.range", "low above high, swapped", (lo, hi), "range_swapped")
            lo, hi = hi, lo
        direction = h.get("direction") if h.get("direction") in ai_constants.OUTLOOK_DIRECTIONS else "flat"
        if direction != h.get("direction"):
            flag(f"{key}.direction", "not up|flat|down, set flat", h.get("direction"), "ledger_direction_invalid")

        p = dict(h.get("p_band") or {})
        probs = {b: max(0.0, float(p.get(b) or 0.0)) for b in S["bands"]}
        total = sum(probs.values())
        if abs(total - 1.0) > 0.01:
            flag(f"{key}.p_band", f"sums to {total:.3f}, renormalised", p, "p_band_renormalised")
            probs = ({b: v / total for b, v in probs.items()} if total > 0
                     else {b: 1.0 / len(probs) for b in probs})
        probs = {b: round(v, 4) for b, v in probs.items()}
        mid_band = band_of((lo + hi) / 2)
        if probs[mid_band] < max(probs.values()):
            flag(f"{key}.p_band", f"midpoint band {mid_band} is not the modal band", probs,
                 "p_band_midpoint_not_modal")
        out[key] = {"direction": direction, "score_low": lo, "score_high": hi, "p_band": probs}

    # --- ledger directions --------------------------------------------------
    ld = dict(parsed.get("ledger_direction") or {})
    cleaned_ld = {}
    for led in ai_constants.LEDGER_FIELDS:
        v = ld.get(led)
        if v not in ai_constants.OUTLOOK_DIRECTIONS:
            flag(f"ledger_direction.{led}", "not up|flat|down, set flat", v, "ledger_direction_invalid")
            v = "flat"
        cleaned_ld[led] = v
    out["ledger_direction"] = cleaned_ld

    # --- drivers ------------------------------------------------------------
    drivers_in = list(parsed.get("drivers") or [])
    if len(drivers_in) > S["max_drivers"]:
        flag("drivers", f"{len(drivers_in)} drivers, kept {S['max_drivers']}", len(drivers_in), "drivers_truncated")
        drivers_in = drivers_in[: S["max_drivers"]]
    drivers: List[Dict[str, Any]] = []
    for i, d in enumerate(drivers_in):
        if not isinstance(d, dict) or d.get("question") not in ai_constants.QUESTION_IDS:
            flag(f"drivers[{i}]", "unknown question, dropped", d, "drivers_dropped")
            continue
        ledger = ai_constants.LEDGER_OF_QUESTION[d["question"]]
        if d.get("ledger") != ledger:
            flag(f"drivers[{i}].ledger", f"{d.get('ledger')} is not {d['question']}'s ledger, corrected",
                 d.get("ledger"), "driver_ledger_corrected")
        evidence = []
        for aid in d.get("evidence") or []:
            if aid in known_ids and aid not in evidence:
                evidence.append(aid)
            else:
                flag(f"drivers[{i}].evidence", "unknown article id, dropped", aid, "driver_evidence_dropped")
        direction = d.get("direction") if d.get("direction") in ai_constants.OUTLOOK_DIRECTIONS else "flat"
        drivers.append({"question": d["question"], "ledger": ledger, "direction": direction,
                        "why": str(d.get("why") or "").strip(), "evidence": evidence})
    out["drivers"] = drivers

    # --- uncertainties and confidence ---------------------------------------
    unc = [str(u).strip() for u in (parsed.get("key_uncertainties") or []) if str(u).strip()]
    if len(unc) > S["max_uncertainties"]:
        flag("key_uncertainties", f"{len(unc)} given, kept {S['max_uncertainties']}", len(unc), "uncertainties_truncated")
        unc = unc[: S["max_uncertainties"]]
    out["key_uncertainties"] = unc
    try:
        conf = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.5
        flag("confidence", "not a number, set 0.5", parsed.get("confidence"), "confidence_clamped")
    if not 0.0 <= conf <= 1.0:
        flag("confidence", "outside 0..1, clamped", conf, "confidence_clamped")
        conf = max(0.0, min(1.0, conf))
    out["confidence"] = round(conf, 3)

    # --- prose --------------------------------------------------------------
    limit = S["max_chars"]["prose"]
    for key in ("base_case", "upside_risk", "downside_risk", "changes_since_last"):
        text = str(parsed.get(key) or "").strip()
        if key != "changes_since_last" and len(text) > limit:
            flag(key, f"{len(text)} chars, clipped to {limit}", len(text), "prose_clipped")
            text = _clip(text, limit)
        out[key] = text

    headed = str(parsed.get("headed_text") or "").strip()
    if "—" in headed:
        flag("headed_text", "em dash replaced", headed.count("—"), "headed_em_dash")
        headed = headed.replace(" — ", ", ").replace("—", ", ")
    hlimit = S["max_chars"]["headed"]
    if len(headed) > hlimit:
        flag("headed_text", f"{len(headed)} chars, clipped to {hlimit}", len(headed), "headed_clipped")
        headed = _clip(headed, hlimit)
    quoted = range_in_text(headed)
    matched = [key for key in ("horizon_4w", "horizon_12w")
               if (out[key]["score_low"], out[key]["score_high"]) in quoted]
    if not matched:
        flag("headed_text", "quotes no range equal to horizon_4w or horizon_12w", quoted, "headed_range_missing")
    else:
        lowered = headed.lower()
        for key in matched:
            words = S["direction_words"][out[key]["direction"]]
            if not any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words):
                flag("headed_text", f"range of {key} quoted without its direction word "
                     f"({out[key]['direction']})", None, "headed_direction_missing")
    out["headed_text"] = headed

    # --- watch resolutions --------------------------------------------------
    resolutions: Dict[str, Dict[str, Any]] = {}
    for i, r in enumerate(parsed.get("watch_resolutions") or []):
        if not isinstance(r, dict):
            continue
        item = r.get("item_id")
        if item == ai_constants.NO_OPEN_ITEMS:
            continue
        if item not in open_ids:
            flag(f"watch_resolutions[{i}]", "item was not open, dropped", item, "watch_resolution_unknown_item")
            continue
        if item in resolutions:
            flag(f"watch_resolutions[{i}]", "second verdict on one item, dropped", item, "watch_resolution_duplicate")
            continue
        outcome = r.get("outcome") if r.get("outcome") in ai_constants.WATCH_OUTCOMES else "still_open"
        evidence = []
        for aid in r.get("evidence") or []:
            if aid in known_ids and aid not in evidence:
                evidence.append(aid)
            else:
                flag(f"watch_resolutions[{i}].evidence", "unknown article id, dropped", aid, "watch_evidence_dropped")
        resolutions[item] = {"item_id": item, "outcome": outcome, "evidence": evidence,
                             "note": str(r.get("note") or "").strip()}
    for item in open_item_ids:
        if item not in resolutions:
            flag("watch_resolutions", "open item not addressed, left still_open", item, "watch_unaddressed")
            resolutions[item] = {"item_id": item, "outcome": "still_open", "evidence": [],
                                 "note": "not addressed by the model"}
    out["watch_resolutions"] = list(resolutions.values())

    # --- new watch items ----------------------------------------------------
    proposed = list(parsed.get("new_watch") or [])
    if len(proposed) > S["max_new_watch"]:
        flag("new_watch", f"{len(proposed)} proposed, kept {S['max_new_watch']}", len(proposed), "new_watch_truncated")
        proposed = proposed[: S["max_new_watch"]]
    horizon_end = run_date + dt.timedelta(days=S["watch_horizon_days"])
    new_watch: List[Dict[str, Any]] = []
    for i, w in enumerate(proposed):
        if not isinstance(w, dict) or not str(w.get("title") or "").strip():
            flag(f"new_watch[{i}]", "no title, dropped", w, "new_watch_dropped")
            continue
        if w.get("question") not in ai_constants.QUESTION_IDS:
            flag(f"new_watch[{i}]", "unknown question, dropped", w.get("question"), "new_watch_dropped")
            continue
        due = _parse_date(w.get("due_date"))
        if w.get("due_date") and due is None:
            flag(f"new_watch[{i}].due_date", "not a strict ISO date, dropped", w.get("due_date"), "new_watch_dropped")
            continue
        if due is not None and not (run_date < due <= horizon_end):
            flag(f"new_watch[{i}].due_date", "outside the 90-day horizon, dropped", w.get("due_date"), "new_watch_dropped")
            continue
        kind = "deadline" if due else "development"
        if w.get("kind") != kind:
            flag(f"new_watch[{i}].kind", f"{w.get('kind')} with{'out' if due is None else ''} a date, set {kind}",
                 w.get("kind"), "new_watch_kind_corrected")
        evidence = []
        for aid in w.get("evidence") or []:
            if aid in known_ids and aid not in evidence:
                evidence.append(aid)
            else:
                flag(f"new_watch[{i}].evidence", "unknown article id, dropped", aid, "new_watch_evidence_dropped")
        direction = w.get("direction") if w.get("direction") in ai_constants.WATCH_DIRECTIONS else "unclear"
        new_watch.append({"kind": kind, "title": str(w["title"]).strip(),
                          "due_date": due.isoformat() if due else None,
                          "question": w["question"], "direction": direction, "evidence": evidence})
    out["new_watch"] = new_watch

    # --- masking ------------------------------------------------------------
    if iso2:
        out = _redact_strings(iso2, out, flag)

    return {"answer": out, "violations": violations, "counts": counts}


def _redact_strings(iso2: str, value: Any, flag, path: str = "") -> Any:
    """Replace any gazetteer term the model wrote back, and count it."""
    if isinstance(value, str):
        hits = gazetteer.scan(iso2, value)
        if hits:
            cleaned, _ = gazetteer.redact(iso2, value)
            flag(path or "<root>", "country term in the model's output, redacted",
                 [t for t, _ in hits], "mask_leak_in_output")
            return cleaned
        return value
    if isinstance(value, dict):
        return {k: _redact_strings(iso2, v, flag, f"{path}.{k}" if path else k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_strings(iso2, v, flag, f"{path}[{i}]") for i, v in enumerate(value)]
    return value
