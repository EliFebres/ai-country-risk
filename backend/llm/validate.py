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
  article id that was sent, a known question, and **the date written in that
  article's text** (day and month, or month and year, in any common form).
  Step 5b found the model giving first-of-month dates to elections whose
  articles gave none.
- A scheduled event that reads as a **routine deadline** (a filing date, a tax
  payment, a statistical release, a regular session, an earnings date) is
  dropped. The prompt says not to list them; step 5c found a tax-payment
  deadline listed in 15 of 15 calls, the only thing moving `score_3m`.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence

from backend.llm import constants as ai_constants
from backend.util import constants as util_constants

__all__ = ["validate", "ValidationResult", "COUNT_KEYS", "ROUTINE_DEADLINE_PATTERNS",
           "article_texts", "title_only_ids", "date_in_text", "is_routine_deadline",
           "normalise_quote_text", "quote_found", "QUOTE_MATCH", "QUOTE_MIN_WORDS"]

COUNT_KEYS = (
    "levels_nulled_no_evidence",
    "levels_nulled_quote_not_found",
    "levels_nulled_title_only",
    "levels_out_of_range",
    "evidence_dropped",
    "evidence_dropped_non_article",
    "events_dropped",
    "events_dropped_date_not_in_article",
    "events_dropped_routine_deadline",
)

#: How a quote is matched against the cited articles' text (step 5e, Fix 3).
#: ``"exact"``: the normalised quote is a substring of one cited article's
#: normalised text. ``"ordered_90"``: at least 90% of the quote's words appear
#: in order in one cited article; the pre-set fallback, switched on only if the
#: exact rule nulls 15–30% of the model's levels and the failures are
#: near-misses. ``"report"``: count and record, never null.
QUOTE_MATCH = "exact"
QUOTE_MIN_WORDS = 4
QUOTE_ORDERED_SHARE = 0.9

_CURLY = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"', "´": "'", "`": "'",
    "–": "-", "—": "-", "−": "-", " ": " ",
})
_EDGE_PUNCT = ".,;:!?\"'()[]{}<>…-/\\|*_~`"


def normalise_quote_text(text: Any) -> str:
    """Lower-case, straighten quotes and dashes, collapse whitespace, strip the
    punctuation at either end. The same function runs on the quote and on the
    article text, so a curly apostrophe on one side cannot fail the match."""
    s = unicodedata.normalize("NFC", str(text or "")).translate(_CURLY).lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(_EDGE_PUNCT + " ")


def _words(text: str) -> List[str]:
    return [w.strip(_EDGE_PUNCT) for w in text.split() if w.strip(_EDGE_PUNCT)]


def _ordered_share(quote_words: List[str], text_words: List[str]) -> float:
    """The share of `quote_words` that appear in `text_words` in order."""
    if not quote_words:
        return 0.0
    hit = 0
    pos = 0
    for w in quote_words:
        try:
            pos = text_words.index(w, pos) + 1
            hit += 1
        except ValueError:
            continue
    return hit / len(quote_words)


def quote_found(quote: Any, texts: Sequence[str], *, mode: Optional[str] = None) -> bool:
    """Whether `quote` is copied from one of `texts`, under `QUOTE_MATCH`.

    A quote shorter than `QUOTE_MIN_WORDS` never counts: four words is the
    floor below which a match says nothing about the article.
    """
    mode = mode or QUOTE_MATCH
    q = normalise_quote_text(quote)
    qw = _words(q)
    if len(qw) < QUOTE_MIN_WORDS:
        return False
    for t in texts:
        nt = normalise_quote_text(t)
        if q in nt:
            return True
        if mode == "ordered_90" and _ordered_share(qw, _words(nt)) >= QUOTE_ORDERED_SHARE:
            return True
    return False

#: An event whose text matches one of these (case-insensitive, anywhere in the
#: text) is a routine, recurring deadline and is dropped. Elections,
#: referendums, court rulings, votes, treaty and sanction dates, summits,
#: strikes, debt maturities and central bank decisions match none of them.
ROUTINE_DEADLINE_PATTERNS = (
    r"deadline",
    r"filing",
    r"due date",
    r"payment date",
    r"tax return",
    r"tax payment",
    r"statistical release",
    r"data release",
    r"release of .* data",
    r"scheduled session",
    r"regular session",
    r"quarterly",
    r"monthly",
    r"annual report",
    r"earnings",
)
_ROUTINE_DEADLINE_RE = re.compile("|".join(f"(?:{p})" for p in ROUTINE_DEADLINE_PATTERNS),
                                  re.IGNORECASE)


def is_routine_deadline(text: str) -> bool:
    """Whether an event's text reads as a routine, recurring deadline."""
    return bool(text) and _ROUTINE_DEADLINE_RE.search(text) is not None

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")


def article_texts(payload: Dict[str, Any]) -> Dict[str, str]:
    """Everything the model could read about each article, keyed by id.

    The digest fields for every article, and the body for the ones sent in
    full. This is what a scheduled event's date is checked against.
    """
    texts: Dict[str, List[str]] = {}
    for a in payload.get("articles") or []:
        parts = [str(a.get("title") or ""), str(a.get("published") or "")]
        digest = a.get("digest") or {}
        for v in digest.values():
            parts.append(" ".join(map(str, v)) if isinstance(v, list) else str(v or ""))
        texts.setdefault(a.get("id"), []).extend(parts)
    for f in payload.get("full_texts") or []:
        texts.setdefault(f.get("id"), []).append(str(f.get("text") or ""))
    return {aid: "\n".join(p) for aid, p in texts.items() if aid}


def title_only_ids(payload: Dict[str, Any]) -> List[str]:
    """The ids of the articles the model saw as a headline alone."""
    return [a.get("id") for a in payload.get("articles") or []
            if a.get("id") and a.get("body_status", "title-only") == "title-only"]


def date_in_text(when: dt.date, text: str) -> bool:
    """Whether `when` is written in `text`, as day-month or month-year.

    Accepts "3 November", "November 3", "Nov. 3rd", "3 Nov 2026",
    "November 2026", "2026-11-03", "3/11/2026" and "11/3/2026". Does not
    accept the year alone, or a month alone.
    """
    if not text:
        return False
    t = text.lower()
    month = _MONTHS[when.month - 1]
    mon = month[:3]
    d, y = when.day, when.year
    month_re = rf"(?:{month}|{mon}\.?)"
    day_re = rf"{d}(?:st|nd|rd|th)?"
    patterns = [
        rf"\b{day_re}\s+(?:of\s+)?{month_re}\b",
        rf"\b{month_re}\s+{day_re}\b",
        rf"\b{month_re},?\s+{y}\b",
        rf"\b{y}-{when.month:02d}-{d:02d}\b",
        rf"\b{d}[/.]{when.month}[/.]{y}\b",
        rf"\b{d:02d}[/.]{when.month:02d}[/.]{y}\b",
        rf"\b{when.month}[/.]{d}[/.]{y}\b",
        rf"\b{when.month:02d}[/.]{d:02d}[/.]{y}\b",
    ]
    return any(re.search(pat, t) for pat in patterns)

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
    texts: Optional[Dict[str, str]] = None,
    title_only: Sequence[str] = (),
) -> ValidationResult:
    """Validate a decoded checklist answer against the full contract.

    Args:
        answer: What the model returned, already JSON-decoded.
        article_ids: The ids that were actually sent, so a citation of an
            article that was never supplied can be dropped.
        run_date: The date the evidence is read as of; the scheduled-event
            horizon is measured from it. Defaults to today.
        texts: `article_texts(payload)`: what the model could read per
            article. When given, a scheduled event is kept only if its date
            is written in the cited article, and a level's quote must be
            copied from one of the articles it cites (`QUOTE_MATCH`). When
            None both checks are skipped.
        title_only: The ids the model saw as a headline alone. A level whose
            only evidence is such articles is nulled and counted; after step
            5e's selection fix it should always be 0 (a tripwire).

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
    headline_only = set(title_only)
    level_max = ai_constants.SCORING["level_max"]
    cleaned: Dict[str, Any] = {"questions": {}}

    for qid in ai_constants.QUESTION_IDS:
        row = answer.get(qid)
        if not isinstance(row, dict):
            violations.append({"field": qid, "problem": "missing", "raw": row})
            cleaned["questions"][qid] = {"level": None, "evidence": [], "quote": "", "note": ""}
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

        quote = row.get("quote")
        quote = quote.strip() if isinstance(quote, str) else ""
        if level is not None and texts is not None:
            # The quote must be copied from an article the level cites. An id
            # that exists is not evidence that the article says anything about
            # the question; the words are (step 5e, Fix 3).
            cited_texts = [texts.get(aid, "") for aid in evidence]
            if not quote_found(quote, cited_texts):
                violations.append({"field": f"{qid}.quote", "problem": "quote not found",
                                   "raw": quote})
                counts["levels_nulled_quote_not_found"] += 1
                if QUOTE_MATCH != "report":
                    level = None

        if level is not None and evidence and all(aid in headline_only for aid in evidence):
            # A level built on eight to twelve words of headline. Selection
            # no longer sends headline-only articles, so this should be 0.
            violations.append({"field": f"{qid}.level", "problem": "title-only evidence",
                               "raw": level})
            counts["levels_nulled_title_only"] += 1
            level = None

        cleaned["questions"][qid] = {
            "level": level,
            "evidence": evidence,
            "quote": quote,
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
        elif texts is not None and not date_in_text(when, texts.get(aid, "")):
            problem = "date not in article"
        elif is_routine_deadline(str(ev.get("event") or "")):
            problem = "routine deadline"
        if problem:
            violations.append({"field": field, "problem": problem, "raw": ev})
            counts["events_dropped"] += 1
            if problem == "date not in article":
                counts["events_dropped_date_not_in_article"] += 1
            elif problem == "routine deadline":
                counts["events_dropped_routine_deadline"] += 1
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
