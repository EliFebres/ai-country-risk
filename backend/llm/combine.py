"""
The arithmetic: from validated checklist levels and data percentiles to a score.

Nothing here calls a model or a database. Every function takes dicts and
returns dicts, so the weekly run and the measurement harness share one path
and the manifest can hold every intermediate value.

    news_ledger   = news_scale × mean(non-null levels in that ledger)
    ledger_score  = w_data × data + w_news × news      (one half alone if the
                                                        other is missing)
    score_12m     = round(Σ composite_weights × ledger_score)
    score_3m      = score_12m + clamp(Σ ±event_step, ±event_cap)
    article_weight = Σ levels of the questions that cite the article

The constants are `constants.SCORING`, which is inside `PROMPT_VERSION`.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence

from backend.llm import constants as ai_constants

__all__ = [
    "combine", "news_scores", "ledger_scores", "composite_12m",
    "event_adjustment", "article_weights", "carry_forward", "merge_answers",
    "round_half_up",
]

S = ai_constants.SCORING
LEDGERS = ai_constants.LEDGER_FIELDS


def round_half_up(x: float) -> int:
    """Python's round() is banker's rounding; a score of 46.5 should be 47."""
    return int(math.floor(x + 0.5))


def _clamp(x: float, low: float, high: float) -> float:
    return max(low, min(high, x))


# --- The news half -----------------------------------------------------------

def news_scores(questions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per ledger: `news_scale × mean(non-null levels)`, or None if all null."""
    out: Dict[str, Dict[str, Any]] = {}
    for ledger in LEDGERS:
        qids = ai_constants.QUESTIONS_BY_LEDGER[ledger]
        levels = {qid: (questions.get(qid) or {}).get("level") for qid in qids}
        answered = [v for v in levels.values() if v is not None]
        out[ledger] = {
            "score": round(S["news_scale"] * sum(answered) / len(answered), 2) if answered else None,
            "levels": levels,
            "n_answered": len(answered),
            "n_questions": len(qids),
        }
    return out


# --- Combining the halves ----------------------------------------------------

def ledger_scores(
    data_scores: Dict[str, Optional[float]],
    news: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """`w_data × data + w_news × news`; one half alone when the other is missing."""
    out: Dict[str, Dict[str, Any]] = {}
    w = S["ledger_weights"]
    for ledger in LEDGERS:
        d = data_scores.get(ledger)
        n = (news.get(ledger) or {}).get("score")
        if d is not None and n is not None:
            score, mode = w["data"] * d + w["news"] * n, "both"
        elif d is not None:
            score, mode = d, "data_only"
        elif n is not None:
            score, mode = n, "news_only"
        else:
            score, mode = None, "none"
        out[ledger] = {
            "score": round(score, 2) if score is not None else None,
            "data": d,
            "news": n,
            "mode": mode,
        }
    return out


def composite_12m(ledgers: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """`round(Σ w × ledger)` over the ledgers with a positive weight.

    A ledger with no score drops out and the remaining weights are renormalised,
    which is the ledger-level rule (data-only, news-only) applied one level up.
    If every weighted ledger is null there is no score, and the country fails
    rather than receiving a number nothing supports.
    """
    weights = {k: v for k, v in S["composite_weights"].items() if v > 0}
    present = {k: ledgers[k]["score"] for k in weights if ledgers.get(k, {}).get("score") is not None}
    if not present:
        raise ValueError("no ledger has a score; the composite has nothing to read")
    total = sum(weights[k] for k in present)
    used = {k: round(weights[k] / total, 4) for k in present}
    raw = sum(used[k] * present[k] for k in present)
    return {
        "score_12m": round_half_up(raw),
        "raw": round(raw, 3),
        "weights_used": used,
        "renormalised": set(present) != set(weights),
        "missing_ledgers": sorted(set(weights) - set(present)),
    }


def event_adjustment(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """`clamp(Σ ±event_step, −event_cap, +event_cap)`; "unclear" counts 0."""
    per_event = []
    total = 0
    for ev in events:
        step = {"up": S["event_step"], "down": -S["event_step"]}.get(ev.get("direction"), 0)
        total += step
        per_event.append({**ev, "step": step})
    return {
        "delta": int(_clamp(total, -S["event_cap"], S["event_cap"])),
        "sum": total,
        "per_event": per_event,
    }


def article_weights(
    questions: Dict[str, Dict[str, Any]],
    article_ids: Sequence[str],
) -> List[Dict[str, Any]]:
    """Every article sent, with the sum of the levels of the questions citing it.

    An uncited article weighs 0. A carried level cites last week's ids with a
    date prefix, which never matches, so carried levels weigh nothing here.
    """
    weight: Dict[str, int] = {aid: 0 for aid in article_ids}
    cited: Dict[str, List[str]] = {aid: [] for aid in article_ids}
    for qid in ai_constants.QUESTION_IDS:
        q = questions.get(qid) or {}
        level = q.get("level")
        if level is None:
            continue
        for aid in q.get("evidence") or []:
            if aid in weight:
                weight[aid] += int(level)
                cited[aid].append(qid)
    return [{"id": aid, "weight": weight[aid], "questions": cited[aid]} for aid in article_ids]


def subscore_text(
    ledger: str,
    scored: Dict[str, Any],
    news: Dict[str, Any],
    percentiles: Dict[str, Dict[str, Any]],
) -> str:
    """One line per ledger for `subscore_evidence`: the numbers behind the score."""
    codes = [c for c, p in percentiles.items() if p.get("ledger") == ledger and p.get("percentile") is not None]
    data_part = (
        f"data {scored['data']:.0f} from {len(codes)} indicator(s): "
        + ", ".join(f"{c} p{percentiles[c]['percentile']:.0f}" for c in codes)
        if scored.get("data") is not None else "no indicator resolved"
    )
    answered = {q: lv for q, lv in news["levels"].items() if lv is not None}
    news_part = (
        f"news {scored['news']:.0f} from {len(answered)} of {news['n_questions']} questions: "
        + ", ".join(f"{q}={lv}" for q, lv in answered.items())
        if scored.get("news") is not None else "every question null"
    )
    score = f"{scored['score']:.0f}" if scored.get("score") is not None else "none"
    return f"{ledger} {score} ({scored['mode']}); {data_part}; {news_part}."


# --- Carry-forward -------------------------------------------------------------

def carry_forward(
    questions: Dict[str, Dict[str, Any]],
    history: Sequence[Dict[str, Any]],
    *,
    run_date: dt.date,
    questions_version: str = ai_constants.QUESTIONS_VERSION,
) -> tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Fill a null question from the most recent original level in recent runs.

    "A quiet week is not a good week": a question nobody wrote about this week
    is not a question that went to zero. The level comes from the newest of
    the last `carry_forward_runs` snapshots, none older than
    `carry_forward_max_days`, whose answers were to the same questions
    (`questions_version`), and only from a level that was observed that week
    rather than itself carried, so nothing is carried for longer than the
    window.

    Args:
        history: `store.read_recent_snapshots(...)` rows, newest first, each
            with `run_date` and `manifest`.

    Returns:
        ``(questions, log)``. Carried entries have ``carried: True``,
        ``carried_from`` and their evidence prefixed with the source date.
    """
    out = {qid: dict(q) for qid, q in questions.items()}
    log: List[Dict[str, Any]] = []
    max_age = dt.timedelta(days=S["carry_forward_max_days"])
    usable = []
    for row in list(history)[: S["carry_forward_runs"]]:
        when = row.get("run_date")
        if isinstance(when, str):
            when = dt.date.fromisoformat(when)
        if when is None or when >= run_date or run_date - when > max_age:
            continue
        scoring = ((row.get("manifest") or {}).get("scoring")) or {}
        if scoring.get("questions_version") != questions_version:
            continue
        usable.append((when, scoring.get("questions") or {}))

    for qid in ai_constants.QUESTION_IDS:
        q = out.setdefault(qid, {"level": None, "evidence": [], "note": ""})
        q.setdefault("carried", False)
        q.setdefault("carried_from", None)
        if q.get("level") is not None:
            continue
        for when, past in usable:  # newest first
            prev = past.get(qid) or {}
            if prev.get("level") is None or prev.get("carried"):
                continue
            q["level"] = prev["level"]
            q["evidence"] = [f"{when.isoformat()}:{aid}" for aid in prev.get("evidence") or []]
            q["note"] = prev.get("note") or ""
            q["carried"] = True
            q["carried_from"] = when.isoformat()
            log.append({"question": qid, "level": prev["level"], "from": when.isoformat()})
            break
    return out, log


# --- Median of three ---------------------------------------------------------

def merge_answers(answers: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The per-question median of several validated answers.

    This is sound where averaging composites was not: each question's levels
    are ordered, so the median is one of the levels given. A question is
    non-null only when at least two calls answered it; with an even count of
    answers the lower middle is taken, since a null vote is "not raised".
    Scheduled events are kept when at least two calls listed the same
    `(article_id, question, direction)`, at the earliest date given. Flags go
    by majority; the summary is the first call's.
    """
    answers = [a for a in answers if a]
    if not answers:
        raise ValueError("no answers to merge")
    k = len(answers)
    questions: Dict[str, Dict[str, Any]] = {}
    votes: Dict[str, List[Optional[int]]] = {}
    for qid in ai_constants.QUESTION_IDS:
        rows = [(a.get("questions") or {}).get(qid) or {} for a in answers]
        levels = [r.get("level") for r in rows]
        votes[qid] = levels
        answered = sorted(v for v in levels if v is not None)
        if len(answered) >= 2:
            level = answered[(len(answered) - 1) // 2]
            evidence = sorted({aid for r in rows if r.get("level") == level for aid in r.get("evidence") or []})
            note = next((r.get("note") or "" for r in rows if r.get("level") == level), "")
        else:
            level, evidence, note = None, [], ""
        questions[qid] = {"level": level, "evidence": evidence, "note": note}

    seen: Dict[tuple, List[Dict[str, Any]]] = {}
    for a in answers:
        for ev in a.get("scheduled_events") or []:
            seen.setdefault((ev.get("article_id"), ev.get("question"), ev.get("direction")), []).append(ev)
    events = []
    for key, copies in seen.items():
        if len(copies) >= 2:
            first = min(copies, key=lambda e: e.get("date") or "")
            events.append(dict(first))

    flags = {}
    for name in ai_constants.CONDITION_FLAGS:
        c = Counter(bool((a.get("condition_flags") or {}).get(name)) for a in answers)
        flags[name] = c[True] > c[False]

    return {
        "questions": questions,
        "scheduled_events": events,
        "condition_flags": flags,
        "bullet_summary": answers[0].get("bullet_summary") or "",
        "merge": {"k": k, "votes": votes},
    }


# --- The whole thing -----------------------------------------------------------

def combine(
    *,
    answer: Dict[str, Any],
    data: Dict[str, Any],
    article_ids: Sequence[str],
    run_date: dt.date,
    history: Optional[Sequence[Dict[str, Any]]] = None,
    validation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """From a validated answer and the data percentiles to every number stored.

    Args:
        answer: `validate.validate(...)["answer"]`, or `merge_answers(...)`.
        data: `data_score.ledger_data_scores(...)`.
        article_ids: The ids sent, for `article_weights`.
        run_date: For the carry-forward window.
        history: Recent snapshots for carry-forward, or None to skip it (the
            frozen payloads have no history).
        validation: `{"violations", "counts"}` from the validator, carried
            into the record.

    Returns:
        The `scoring` dict stored whole in `manifest["scoring"]`.

    Raises:
        ValueError: when no weighted ledger has a score.
    """
    questions = {qid: dict(q) for qid, q in (answer.get("questions") or {}).items()}
    for qid in ai_constants.QUESTION_IDS:
        questions.setdefault(qid, {"level": None, "evidence": [], "note": ""})
        questions[qid].setdefault("carried", False)
        questions[qid].setdefault("carried_from", None)

    carry_log: List[Dict[str, Any]] = []
    if history is not None:
        questions, carry_log = carry_forward(questions, history, run_date=run_date)

    news = news_scores(questions)
    ledgers = ledger_scores(data.get("data_scores") or {}, news)
    composite = composite_12m(ledgers)
    events = event_adjustment(answer.get("scheduled_events") or [])
    score_3m = int(_clamp(composite["score_12m"] + events["delta"], 0, 100))
    weights = article_weights(questions, article_ids)

    return {
        "prompt_version": ai_constants.PROMPT_VERSION,
        "questions_version": ai_constants.QUESTIONS_VERSION,
        "constants": S,
        "roster_sha256": data.get("roster_sha256"),
        "percentiles": data.get("percentiles") or {},
        "data_scores": data.get("data_scores") or {},
        "n_indicators_by_ledger": data.get("n_by_ledger") or {},
        "questions": questions,
        "news_scores": news,
        "ledgers": ledgers,
        "composite": composite,
        "events": {
            "valid": answer.get("scheduled_events") or [],
            "delta": events["delta"],
            "sum": events["sum"],
            "per_event": events["per_event"],
            "score_3m": score_3m,
        },
        "score_12m": composite["score_12m"],
        "score_3m": score_3m,
        "ledger_ints": {
            k: (round_half_up(v["score"]) if v["score"] is not None else None)
            for k, v in ledgers.items()
        },
        "article_weights": weights,
        "subscore_text": {
            ledger: subscore_text(ledger, ledgers[ledger], news[ledger], data.get("percentiles") or {})
            for ledger in LEDGERS
        },
        "condition_flags": answer.get("condition_flags") or {},
        "bullet_summary": answer.get("bullet_summary") or "",
        "validation": validation or {},
        "carry_log": carry_log,
        "merge": answer.get("merge"),
    }
