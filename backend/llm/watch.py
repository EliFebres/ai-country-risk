"""
The watch list: a list that remembers.

The screen does not show "what the model said this week". It shows the live
state of a list that is fed every week, resolved every week, and ages. An item
arrives from one of three places: the scorer's dated events, the outlook's
proposals, or the economic calendar. It gets a stable id from what it is and
roughly when, so the same vote seen in two weeks is one row seen twice, and it
leaves the list in exactly one of four ways: the model saw it happen or saw it
not happen, a later-dated twin replaced it, or the clock ran out.

Code applies the clock and computes importance. The model only says what it
saw. That split is deliberate: a model asked "is this still worth watching"
says yes to everything, and a model asked "did this happen, and where" has to
cite an article.

    new id --ingest--> open <--re-seen (seen_count+1, last_seen, importance)
                        |
       model happened --+--> happened       (evidence: note + article ids)
       model did_not ---+--> did_not_happen
       same title+question, later date ingested --> superseded (superseded_by)
       window_end passed, no verdict ---------------> expired
       calendar: actual seen before the prune ------> happened; else expired
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

import backend.llm.constants as ai_constants
from backend.data_upsert import store
from backend.util import constants
from backend.util.hashing import content_hash

__all__ = [
    "normalize_title", "anchor_week", "item_id", "window_end", "importance",
    "ingest", "resolve", "confirm_calendar", "score_and_rank", "run", "top_items",
]

S = ai_constants.OUTLOOK_SCORING
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


# --- identity ---------------------------------------------------------------


def normalize_title(title: str) -> str:
    """Lowercase, no punctuation, one space: the title as the hash sees it."""
    return _SPACE.sub(" ", _PUNCT.sub(" ", (title or "").lower())).strip()


def anchor_week(d: dt.date) -> dt.date:
    """The Monday of `d`'s week. A date rounded to the week, so a vote reported
    as the 12th by one article and the 13th by another is one item."""
    return d - dt.timedelta(days=d.weekday())


def item_id(iso2: str, kind: str, title: str, anchor: dt.date) -> str:
    """The stable id of (country, kind, normalized title, anchor week)."""
    return content_hash(f"{iso2}|{kind}|{normalize_title(title)}|{anchor.isoformat()}")


def window_end(kind: str, due_date: Optional[dt.date], first_seen: dt.date) -> dt.date:
    """When an open item expires: due date plus grace, or first seen plus the
    development expiry when there is no date."""
    if kind == "development" or due_date is None:
        return first_seen + dt.timedelta(days=S["development_expiry_days"])
    return due_date + dt.timedelta(days=S["grace_days"])


# --- importance -------------------------------------------------------------


def _question_weight(question: Optional[str]) -> float:
    """The ledger's composite weight, scaled so friction and order are 1.
    Edge is shown but ranks last; a calendar row with no question sits in the
    middle."""
    if not question:
        return 0.5
    ledger = ai_constants.LEDGER_OF_QUESTION.get(question)
    weight = ai_constants.SCORING["composite_weights"].get(ledger, 0.0)
    if weight <= 0:
        return S["edge_question_weight"]
    return weight / max(ai_constants.SCORING["composite_weights"].values())


def _nearness(kind: str, due_date: Optional[dt.date], run_date: dt.date) -> float:
    if kind == "development" or due_date is None:
        return S["development_nearness"]
    days = (due_date - run_date).days
    near, far = S["nearness_near_days"], S["nearness_far_days"]
    if days <= near:
        return 1.0
    if days >= far:
        return 0.0
    return 1.0 - (days - near) / (far - near)


def importance(item: Dict[str, Any], run_date: dt.date) -> float:
    """0.4 nearness + 0.3 question weight + 0.2 persistence + 0.1 direction clarity."""
    w = S["importance_weights"]
    due = item.get("due_date")
    if isinstance(due, str):
        due = dt.date.fromisoformat(due)
    persistence = min(int(item.get("seen_count") or 1), S["persistence_cap"]) / S["persistence_cap"]
    clarity = 1.0 if item.get("expected_direction") in ("up", "down") else 0.0
    score = (w["nearness"] * _nearness(item.get("kind"), due, run_date)
             + w["question"] * _question_weight(item.get("question"))
             + w["persistence"] * persistence
             + w["clarity"] * clarity)
    return round(score, 4)


# --- ingest -----------------------------------------------------------------


def _row(iso2: str, run_date: dt.date, *, kind: str, title: str, due_date: Optional[dt.date],
         question: Optional[str], direction: Optional[str], source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Stored as written: the screen shows the title, and the outlook payload
    # is redacted as a whole before it is sent, so a calendar row's "Fed"
    # reaches the analyst named and the model as "[the country]".
    title = (title or "").strip()
    if not title:
        return None
    anchor = anchor_week(due_date) if due_date else anchor_week(run_date)
    row = {
        "country_iso2": iso2,
        "item_id": item_id(iso2, kind, title, anchor),
        "kind": kind,
        "title": title[:300],
        "due_date": due_date,
        "window_end": window_end(kind, due_date, run_date),
        "question": question,
        "expected_direction": direction if direction in ai_constants.WATCH_DIRECTIONS else "unclear",
        "source": source,
        "first_seen_run_date": run_date,
        "last_seen_run_date": run_date,
        "seen_count": 1,
    }
    row["importance"] = importance(row, run_date)
    return row


def _date(value: Any) -> Optional[dt.date]:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def ingest(
    iso2: str, run_date: dt.date, *,
    scheduled_events: Sequence[Dict[str, Any]] = (),
    new_watch: Sequence[Dict[str, Any]] = (),
    calendar_rows: Sequence[Dict[str, Any]] = (),
) -> List[Dict[str, Any]]:
    """This week's candidates as `watch_item` rows, deduplicated by id."""
    rows: Dict[str, Dict[str, Any]] = {}

    def add(row: Optional[Dict[str, Any]]) -> None:
        if row and row["item_id"] not in rows:
            rows[row["item_id"]] = row

    for ev in scheduled_events:
        add(_row(iso2, run_date, kind="scheduled_event", title=ev.get("event") or "",
                 due_date=_date(ev.get("date")), question=ev.get("question"),
                 direction=ev.get("direction"),
                 source={"article_ids": [ev.get("article_id")] if ev.get("article_id") else [],
                         "from": "scorer"}))
    for w in new_watch:
        add(_row(iso2, run_date, kind=w.get("kind") or "development", title=w.get("title") or "",
                 due_date=_date(w.get("due_date")), question=w.get("question"),
                 direction=w.get("direction"),
                 source={"article_ids": list(w.get("evidence") or []), "from": "outlook"}))
    for c in calendar_rows:
        when = _date(c.get("event_time"))
        add(_row(iso2, run_date, kind="calendar", title=c.get("event") or "", due_date=when,
                 question=None, direction="unclear",
                 source={"event_id": c.get("event_id"), "event_time": str(c.get("event_time")),
                         "importance": c.get("importance"), "from": "calendar"}))
    return list(rows.values())


# --- resolve ----------------------------------------------------------------


def _supersessions(open_items: Sequence[Dict[str, Any]], ingested: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """old item_id -> new item_id where a new item is the same title and
    question with a later date."""
    out: Dict[str, str] = {}
    for old in open_items:
        if not old.get("due_date"):
            continue
        key = (normalize_title(old["title"]), old.get("question"))
        for new in ingested:
            if new["item_id"] == old["item_id"] or not new.get("due_date"):
                continue
            if (normalize_title(new["title"]), new.get("question")) == key and new["due_date"] > old["due_date"]:
                out[old["item_id"]] = new["item_id"]
                break
    return out


def resolve(
    iso2: str, run_date: dt.date, *,
    model_resolutions: Sequence[Dict[str, Any]] = (),
    supersessions: Optional[Dict[str, str]] = None,
) -> Dict[str, int]:
    """Close what can be closed, in this order: the model's verdicts, then
    supersession, then the clock. Returns counts by outcome."""
    counts = {"happened": 0, "did_not_happen": 0, "superseded": 0, "expired": 0,
              "still_open": 0, "calendar_verdicts_ignored": 0}
    open_items = {i["item_id"]: i for i in store.read_watch_items(iso2, status="open", first_seen_until=run_date)}

    for res in model_resolutions:
        item = open_items.get(res.get("item_id"))
        if not item:
            continue
        outcome = res.get("outcome")
        if outcome not in ("happened", "did_not_happen"):
            continue
        if item["kind"] == "calendar":
            # A release either shows an actual in the feed or it does not;
            # the model's reading of an article is not the confirmation.
            counts["calendar_verdicts_ignored"] += 1
            continue
        if store.resolve_watch_item(
            iso2, item["item_id"], status=outcome, resolved_run_date=run_date,
            note=res.get("note"), evidence={"article_ids": res.get("evidence") or [], "by": "outlook"},
        ):
            counts[outcome] += 1
            open_items.pop(item["item_id"])

    for old_id, new_id in (supersessions or {}).items():
        if old_id in open_items and store.resolve_watch_item(
            iso2, old_id, status="superseded", resolved_run_date=run_date,
            note="a later-dated item with the same title and question arrived",
            evidence={"superseded_by": new_id}, superseded_by=new_id,
        ):
            counts["superseded"] += 1
            open_items.pop(old_id)

    for item in list(open_items.values()):
        if item["window_end"] < run_date:
            note = ("calendar row pruned before confirmation" if item["kind"] == "calendar"
                    else "window passed without resolution")
            if store.resolve_watch_item(iso2, item["item_id"], status="expired",
                                        resolved_run_date=run_date, note=note, evidence=None):
                counts["expired"] += 1
                open_items.pop(item["item_id"])

    counts["still_open"] = len(open_items)
    return counts


def confirm_calendar(run_date: dt.date, countries: Optional[Iterable[str]] = None) -> int:
    """Mark open calendar items `happened` when the feed shows an actual.

    Must run before the calendar upsert prunes past rows: that is the one
    moment a release's actual is visible. Returns how many were confirmed.
    """
    confirmed = 0
    now = dt.datetime.now(dt.timezone.utc)
    for iso2 in countries or [c["iso2"] for c in constants.COUNTRY_ROSTER]:
        items = [i for i in store.read_watch_items(iso2, status="open") if i["kind"] == "calendar"]
        if not items:
            continue
        rows = store.read_calendar_rows_by_id([(i.get("source") or {}).get("event_id") for i in items])
        for item in items:
            row = rows.get((item.get("source") or {}).get("event_id"))
            if not row or row.get("actual") in (None, "") or row["event_time"] > now:
                continue
            if store.resolve_watch_item(
                iso2, item["item_id"], status="happened", resolved_run_date=run_date,
                note="release confirmed by the calendar feed",
                evidence={"actual": row.get("actual"), "estimate": row.get("estimate"),
                          "previous": row.get("previous"), "by": "calendar"},
            ):
                confirmed += 1
    return confirmed


# --- rank -------------------------------------------------------------------


def top_items(open_items: Sequence[Dict[str, Any]], n: Optional[int] = None) -> List[Dict[str, Any]]:
    """The displayed items: most important first, dated before undated on ties."""
    n = n or S["display_count"]
    return sorted(
        open_items,
        key=lambda i: (-(i.get("importance") or 0.0), i.get("due_date") is None,
                       i.get("due_date") or dt.date.max, i.get("title") or ""),
    )[:n]


def score_and_rank(iso2: str, run_date: dt.date) -> List[Dict[str, Any]]:
    """Recompute importance for every open item, store it, and mark the top
    five as shown. Returns the top five."""
    open_items = store.read_watch_items(iso2, status="open", first_seen_until=run_date)
    scores = {}
    for item in open_items:
        item["importance"] = importance(item, run_date)
        scores[item["item_id"]] = item["importance"]
    store.update_watch_importance(iso2, scores)
    top = top_items(open_items)
    store.mark_watch_shown(iso2, [i["item_id"] for i in top])
    return top


# --- the week ---------------------------------------------------------------


def run(
    iso2: str, run_date: dt.date, *,
    scheduled_events: Sequence[Dict[str, Any]] = (),
    new_watch: Sequence[Dict[str, Any]] = (),
    watch_resolutions: Sequence[Dict[str, Any]] = (),
    calendar_rows: Sequence[Dict[str, Any]] = (),
    record: bool = True,
) -> Dict[str, Any]:
    """One country's watch-list week: ingest, resolve, score, record.

    Returns the counts and the top five, and writes them to `run_ledger` as
    `job_type='watch'`.
    """
    open_before = store.read_watch_items(iso2, status="open", first_seen_until=run_date)
    rows = ingest(iso2, run_date, scheduled_events=scheduled_events,
                  new_watch=new_watch, calendar_rows=calendar_rows)
    supersessions = _supersessions(open_before, rows)
    carried_from = {new: old for old, new in supersessions.items()}
    for row in rows:
        if row["item_id"] in carried_from:
            row["source"] = {**(row["source"] or {}), "carried_from": carried_from[row["item_id"]]}
    upserted = store.upsert_watch_items(rows) if rows else {"inserted": 0, "reseen": 0}
    resolved = resolve(iso2, run_date, model_resolutions=watch_resolutions, supersessions=supersessions)
    top = score_and_rank(iso2, run_date)

    counts = {
        "ingested": len(rows),
        "inserted": upserted["inserted"],
        "reseen": upserted["reseen"],
        **resolved,
        "open_total": resolved["still_open"],
        "shown": [i["item_id"] for i in top],
    }
    if record:
        try:
            store.write_ledger(job_type="watch", country_iso2=iso2, run_date=run_date,
                               status="ok", detail=counts)
        except Exception as exc:  # the ledger line is a record, not the work
            print(f"[watch] {iso2} ledger not recorded: {exc}")
    print(f"[watch] {iso2} {run_date}: ingested {counts['ingested']} "
          f"(new {counts['inserted']}, re-seen {counts['reseen']}), happened {counts['happened']}, "
          f"did not {counts['did_not_happen']}, expired {counts['expired']}, "
          f"superseded {counts['superseded']}, open {counts['open_total']}")
    return {"counts": counts, "top": top}
