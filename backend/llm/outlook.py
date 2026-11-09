"""
The outlook: a forecaster beside the scorer.

One model call per country per week, after the scorer has written the
snapshot. The model is told it is a forecaster and that it is being measured.
Its input is assembled here, by code, in seven blocks whose order is the order
an analyst would read them: this week's reading, the trend with the arithmetic
already done, the data half, the dated items (scheduled events, the open watch
list, the calendar), its own previous outlook and that outlook's grade, the
base rates across the roster with the scoring instrument's noise floor, and
the calibration memo the ledger has written about it.

Every reader filters `run_date <= as_of`, so the same function serves the live
run and the backfill: an outlook for a past week is assembled from what the
database knew that week, and the manifest says where each block came from.

Masking is production. The payload is redacted by code before it is sent
(`gazetteer.redact` on every string, the replacements recorded) and the
gazetteer tripwire runs on the result, so a term that survives raises rather
than ships.

    python -m backend.llm.outlook backfill [--dry-run] [--limit N] [--no-arms]
    python -m backend.llm.outlook arm --variant memo+anchors --country PT
    python -m backend.llm.outlook probe --country PT [--date 2026-11-09]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import backend.llm.constants as ai_constants
from backend.data_upsert import store
from backend.llm import forecast_eval, langchain_llm, outlook_validate, watch
from backend.util import constants, db, gazetteer, provenance, usage
from backend.util.hashing import content_hash

__all__ = [
    "OUTLOOK_MODEL", "PRODUCTION_VARIANT", "OUTLOOK_VARIANTS",
    "assemble_outlook_payload", "run", "dated_inputs", "backfill", "probe",
]

#: The outlook's model. The scorer's, so the two instruments share one price
#: and one behaviour; it lives here rather than beside SCORING_MODEL because
#: pipeline imports this module and an invariant test counts that literal in
#: pipeline.py alone.
OUTLOOK_MODEL = "gpt-4o-2024-08-06"
OUTPUT_TOKENS_PER_CALL = 1500

#: Named prompt variants. One is production; the others run on the test set
#: as diagnostics (`snapshot_diagnostic kind='outlook-arm'`) and are graded by
#: the same ledger. Promotion is a one-line change to PRODUCTION_VARIANT after
#: the numbers have been read; nothing promotes itself.
PRODUCTION_VARIANT = "memo"
OUTLOOK_VARIANTS: Dict[str, Dict[str, str]] = {
    "memo": {
        "suffix": "",
        "description": "the prompt as written, with the calibration memo in block 7",
    },
    "memo+anchors": {
        "suffix": (
            "\n\nAnchors. Start every range from the base rates block: take the median "
            "absolute 4-week (or 12-week) change in score_12m across the roster as your "
            "default half-width around the current score, widen it when your confidence "
            "is below 0.6, and move the centre only as far as a named driver justifies. "
            "In base_case say which anchor you started from and why you moved off it."
        ),
        "description": "the production prompt plus the roster base rates as explicit anchors for the range",
    },
}

S = ai_constants.OUTLOOK_SCORING
ARM_KIND = "outlook-arm"


# --- small helpers ----------------------------------------------------------


def _d(value: Any) -> Optional[dt.date]:
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


def _iso(value: Any) -> Optional[str]:
    d = _d(value)
    return d.isoformat() if d else None


def _short_question(qid: str) -> str:
    text = ai_constants.QUESTION_TEXT.get(qid, "")
    head = text.split(". ", 1)[0]
    return head if head.endswith(".") else head + "."


def _weeks(a: dt.date, b: dt.date) -> float:
    return round((a - b).days / 7, 1)


def _redact_payload(iso2: str, value: Any, path: str = "", log: Optional[List[Dict[str, Any]]] = None) -> Tuple[Any, List[Dict[str, Any]]]:
    """Replace every gazetteer term in every string, recording where."""
    if log is None:
        log = []
    if isinstance(value, str):
        hits = gazetteer.scan(iso2, value)
        if hits:
            cleaned, _ = gazetteer.redact(iso2, value)
            log.append({"path": path, "terms": sorted({t for t, _ in hits})})
            return cleaned, log
        return value, log
    if isinstance(value, dict):
        return {k: _redact_payload(iso2, v, f"{path}.{k}" if path else str(k), log)[0]
                for k, v in value.items()}, log
    if isinstance(value, (list, tuple)):
        return [_redact_payload(iso2, v, f"{path}[{i}]", log)[0] for i, v in enumerate(value)], log
    return value, log


def _serialize(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


# --- the seven blocks -------------------------------------------------------


def _articles(iso2: str, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    selected = manifest.get("selected") or []
    by_url = store.read_articles([a.get("url") for a in selected if a.get("url")])
    out = []
    for a in selected:
        art = by_url.get(a.get("url")) or {}
        out.append({
            "id": a.get("id"),
            "title": art.get("title") or "",
            "publisher": art.get("publisher"),
            "published": _iso(art.get("published_at") or art.get("page_published_at")),
            "themes": a.get("themes") or [],
            "body_status": a.get("body_status"),
        })
    return out


def _block_reading(snapshot: Dict[str, Any], scoring: Dict[str, Any], articles: List[Dict[str, Any]]) -> Dict[str, Any]:
    questions = scoring.get("questions") or {}
    checklist = {}
    for qid in ai_constants.QUESTION_IDS:
        q = questions.get(qid) or {}
        checklist[qid] = {
            "ledger": ai_constants.LEDGER_OF_QUESTION[qid],
            "asks": _short_question(qid),
            "level": q.get("level"),
            "evidence": q.get("evidence") or [],
            "quote": q.get("quote") or "",
            "note": q.get("note") or "",
            "carried_from_earlier_week": bool(q.get("carried")),
        }
    return {
        "run_date": _iso(snapshot["run_date"]),
        "score_12m": snapshot["score_12m"],
        "score_3m": snapshot["score_3m"],
        "ledgers": {
            "friction": snapshot.get("friction_score"),
            "order": snapshot.get("order_score"),
            "information": snapshot.get("information_score"),
            "edge": snapshot.get("edge_score"),
        },
        "condition_flags": snapshot.get("condition_flags") or {},
        "bullet_summary": snapshot.get("bullet_summary") or "",
        "evidence_coverage": snapshot.get("evidence_coverage"),
        "level_scale": "levels run 0 (calm) to 4 (most severe); null means this week's evidence did not answer the question",
        "checklist": checklist,
        "articles": articles,
    }


def _block_trend(series: List[Dict[str, Any]], history: List[Dict[str, Any]], run_date: dt.date) -> Dict[str, Any]:
    ledgers = ("friction", "order", "information", "edge")
    points = [{
        "run_date": _iso(p["run_date"]),
        "weeks_ago": _weeks(run_date, p["run_date"]),
        "score_12m": p["score_12m"], "score_3m": p["score_3m"],
        **{led: p.get(f"{led}_score") for led in ledgers},
    } for p in series]

    def change(days: int) -> Dict[str, Any]:
        target = run_date - dt.timedelta(days=days)
        cands = [p for p in series if p["run_date"] <= target]
        if not cands or not series:
            return {"available": False, "note": f"no snapshot {days} or more days before this one"}
        ref, cur = cands[-1], series[-1]
        return {
            "available": True,
            "from_run_date": _iso(ref["run_date"]),
            "gap_days": (run_date - ref["run_date"]).days,
            "score_12m": cur["score_12m"] - ref["score_12m"],
            "score_3m": cur["score_3m"] - ref["score_3m"],
            "ledgers": {led: (None if cur.get(f"{led}_score") is None or ref.get(f"{led}_score") is None
                              else cur[f"{led}_score"] - ref[f"{led}_score"]) for led in ledgers},
        }

    levels: Dict[str, List[Dict[str, Any]]] = {qid: [] for qid in ai_constants.QUESTION_IDS}
    for h in sorted(history, key=lambda r: r["run_date"]):
        qs = ((h.get("manifest") or {}).get("scoring") or {}).get("questions") or {}
        for qid in ai_constants.QUESTION_IDS:
            q = qs.get(qid) or {}
            levels[qid].append({"run_date": _iso(h["run_date"]), "level": q.get("level"),
                                "carried": bool(q.get("carried"))})
    scores = [p["score_12m"] for p in series]
    return {
        "n_snapshots": len(series),
        "span_days": (series[-1]["run_date"] - series[0]["run_date"]).days if series else 0,
        "score_12m_min": min(scores) if scores else None,
        "score_12m_max": max(scores) if scores else None,
        "points": points,
        "change_4w": change(S["horizons_days"]["4w"]),
        "change_12w": change(S["horizons_days"]["12w"]),
        "question_levels_by_week": levels,
        "note": "computed by code over this country's stored snapshots; nothing here needs arithmetic",
    }


def _block_data(manifest: Dict[str, Any], scoring: Dict[str, Any], prev_manifest: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    indicators = scoring.get("indicators") or {}
    prev = ((prev_manifest or {}).get("scoring") or {}).get("indicators") or {}
    as_of = {i.get("code"): i for i in manifest.get("indicators") or []}
    entries = []
    for code, e in indicators.items():
        spec = constants.INDICATOR_REGISTRY.get(code) or {}
        p = prev.get(code)
        delta = None
        if p and p.get("value") is not None and e.get("value") is not None:
            delta = {
                "value": round(e["value"] - p["value"], 4),
                "period": (e.get("period") or 0) - (p.get("period") or 0),
                "risk_score": round((e.get("score") or 0) - (p.get("score") or 0), 2),
            }
        entries.append({
            "code": code,
            "label": spec.get("label") or spec.get("name") or code,
            "ledger": e.get("ledger"),
            "period": e.get("period"),
            "as_of": (as_of.get(code) or {}).get("as_of"),
            "value": e.get("value"),
            "unit": spec.get("unit"),
            "risk_score": e.get("score"),
            "change_vs_previous_snapshot": delta,
        })
    return {
        "data_scores_by_ledger": scoring.get("data_scores") or {},
        "n_indicators_by_ledger": scoring.get("n_indicators_by_ledger") or {},
        "indicators": entries,
        "unscored_codes": scoring.get("unscored_indicators") or [],
        "ledger_text": scoring.get("subscore_text") or {},
        "note": "the indicators that resolved this week, with the change against the previous snapshot where one exists",
    }


def _calendar_rows(iso2: str, run_date: dt.date) -> Tuple[List[Dict[str, Any]], str]:
    if iso2 not in constants.FMP_CALENDAR_COUNTRIES:
        return [], "not available (country not in the FMP calendar allowlist)"
    rows = store.read_calendar_events(
        iso2, start=run_date, end=run_date + dt.timedelta(days=S["calendar_horizon_days"]),
        created_before=run_date,
    )
    return rows, ("available" if rows else "not available (no rows knowable on this date)")


def _block_dated(scoring: Dict[str, Any], open_items: List[Dict[str, Any]],
                 calendar_rows: List[Dict[str, Any]], calendar_status: str, run_date: dt.date) -> Dict[str, Any]:
    events = (scoring.get("events") or {}).get("valid") or []
    items = [{
        "item_id": i["item_id"], "kind": i["kind"], "title": i["title"],
        "due_date": _iso(i.get("due_date")), "window_end": _iso(i.get("window_end")),
        "question": i.get("question"), "expected_direction": i.get("expected_direction"),
        "importance": i.get("importance"), "first_seen": _iso(i["first_seen_run_date"]),
        "weeks_open": _weeks(run_date, i["first_seen_run_date"]), "seen_count": i.get("seen_count"),
    } for i in open_items]
    cal = [{
        "event_id": c["event_id"], "date": _iso(c["event_time"]), "event": c["event"],
        "importance": c.get("importance"), "estimate": c.get("estimate"), "previous": c.get("previous"),
    } for c in calendar_rows]
    return {
        "scheduled_events_this_week": events,
        "open_watch_items": items,
        "calendar_next_90_days": cal,
        "calendar_status": calendar_status,
    }


_PREV_FIELDS = ("run_date", "horizon_4w", "horizon_12w", "ledger_direction", "drivers", "base_case",
                "upside_risk", "downside_risk", "headed_text", "confidence", "key_uncertainties",
                "changes_since_last")


def _block_memory(iso2: str, run_date: dt.date) -> Tuple[Dict[str, Any], Optional[dt.date]]:
    prev = store.read_latest_outlook(iso2, before=run_date)
    if not prev:
        return {"previous_outlook": None, "evaluations": [], "note": "first outlook"}, None
    view = {k: (_iso(prev[k]) if k == "run_date" else prev.get(k)) for k in _PREV_FIELDS}
    view["weeks_ago"] = _weeks(run_date, prev["run_date"])
    evals = []
    for d in store.read_diagnostics(kind=forecast_eval.EVAL_KIND, country_iso2=iso2, run_date=prev["run_date"]):
        p = d.get("payload") or {}
        realized_on = _d(p.get("realized_run_date"))
        if d["variant"] in forecast_eval.PRODUCTION_VARIANTS and realized_on and realized_on <= run_date:
            evals.append({
                "horizon": d["variant"], "realized": p.get("realized"), "range_hit": p.get("range_hit"),
                "direction_hit": p.get("direction_hit"), "abs_error_to_midpoint": p.get("abs_error_to_midpoint"),
                "brier": p.get("brier"), "ledger_direction_hits": p.get("ledger_direction_hits"),
            })
    note = ("what the previous outlook predicted, and how it was graded" if evals
            else "what the previous outlook predicted; it has not matured yet")
    return {"previous_outlook": view, "evaluations": evals, "note": note}, prev["run_date"]


def _quantile(values: List[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo), 2)


def _block_base_rates(run_date: dt.date, noise: Dict[str, Any]) -> Dict[str, Any]:
    rows = store.read_roster_scores(until=run_date)
    by_country: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_country.setdefault(r["country_iso2"], []).append(r)
    changes: Dict[str, List[int]] = {"4w": [], "12w": []}
    for series in by_country.values():
        series.sort(key=lambda r: r["run_date"])
        for i in range(len(series)):
            for j in range(i + 1, len(series)):
                gap = (series[j]["run_date"] - series[i]["run_date"]).days
                for h, (lo, hi) in S["base_rate_gap_days"].items():
                    if lo <= gap <= hi:
                        changes[h].append(series[j]["score_12m"] - series[i]["score_12m"])
    floor = noise.get("floor_12m")

    def stats(vals: List[int], h: str) -> Dict[str, Any]:
        lo, hi = S["base_rate_gap_days"][h]
        if not vals:
            return {"n_pairs": 0, "note": f"no two snapshots of one country {lo}-{hi} days apart on record yet"}
        abs_vals = [abs(v) for v in vals]
        return {
            "n_pairs": len(vals),
            "median_change": round(statistics.median(vals), 2),
            "iqr_change": [_quantile([float(v) for v in vals], 0.25), _quantile([float(v) for v in vals], 0.75)],
            "p90_abs_change": _quantile([float(v) for v in abs_vals], 0.9),
            "share_beyond_noise_floor": (round(sum(1 for v in abs_vals if v > floor) / len(vals), 3)
                                         if floor is not None else None),
        }

    return {
        "score_12m_change_4w": stats(changes["4w"], "4w"),
        "score_12m_change_12w": stats(changes["12w"], "12w"),
        "noise_floor": noise,
        "countries_on_record": len(by_country),
        "snapshots_on_record": len(rows),
        "note": "computed by code over every country and every week on record up to this date",
    }


# --- assembly ---------------------------------------------------------------


def dated_inputs(iso2: str, run_date: dt.date) -> Dict[str, Any]:
    """This week's scheduled events and calendar rows, for the watch list when
    the outlook row already existed and nothing was assembled."""
    snapshot = store.read_snapshot(iso2, run_date) or {}
    scoring = (snapshot.get("manifest") or {}).get("scoring") or {}
    rows, status = _calendar_rows(iso2, run_date)
    return {"scheduled_events": (scoring.get("events") or {}).get("valid") or [],
            "calendar_rows": rows, "calendar_status": status}


def assemble_outlook_payload(iso2: str, run_date: dt.date) -> Dict[str, Any]:
    """Everything the outlook model reads for one country on one date.

    Raises:
        LookupError: when the country has no snapshot on `run_date`.
        gazetteer.MaskLeak: when a country term survives redaction.
    """
    snapshot = store.read_snapshot(iso2, run_date)
    if not snapshot:
        raise LookupError(f"{iso2} has no snapshot on {run_date}; the outlook reads the week's score")
    manifest = snapshot.get("manifest") or {}
    scoring = manifest.get("scoring") or {}

    weeks = S["trend_weeks"]
    history = store.read_recent_snapshots(iso2, before=run_date + dt.timedelta(days=1), limit=weeks)
    prev_manifest = next((h["manifest"] for h in history if h["run_date"] < run_date), None)
    series = store.read_snapshot_series(iso2, since=run_date - dt.timedelta(days=7 * weeks), until=run_date)
    articles = _articles(iso2, manifest)
    open_items = store.read_watch_items(iso2, status="open", first_seen_until=run_date)
    calendar_rows, calendar_status = _calendar_rows(iso2, run_date)
    memory, prev_run_date = _block_memory(iso2, run_date)
    noise = forecast_eval.noise_floor()
    memo = store.read_latest_artifact(kind="lessons", created_before=run_date)
    lessons_version = memo["content_sha256"] if memo else None

    payload = {
        "reading": _block_reading(snapshot, scoring, articles),
        "trend": _block_trend(series, history, run_date),
        "data": _block_data(manifest, scoring, prev_manifest),
        "dated_items": _block_dated(scoring, open_items, calendar_rows, calendar_status, run_date),
        "memory": memory,
        "base_rates": _block_base_rates(run_date, noise),
        "calibration_memo": ({"text": (memo["payload"] or {}).get("text"), "version": lessons_version}
                             if memo else {"text": "no evaluated history yet", "version": None}),
    }
    payload, redactions = _redact_payload(iso2, payload)
    gazetteer.assert_clean(iso2, payload)

    from backend.llm.payload import count_tokens
    serialized = _serialize(payload)
    inputs_fingerprint = content_hash(json.dumps({
        "snapshot_fingerprint": snapshot.get("payload_fingerprint"),
        "series": [_iso(p["run_date"]) for p in series],
        "open_items": sorted((i["item_id"], _iso(i["last_seen_run_date"])) for i in open_items),
        "calendar": sorted(c["event_id"] for c in calendar_rows),
        "previous_outlook": _iso(prev_run_date),
        "lessons_version": lessons_version,
    }, sort_keys=True, default=str))

    return {
        "payload": payload,
        "serialized": serialized,
        "payload_tokens": int(count_tokens(serialized)["tokens"]),
        "payload_fingerprint": content_hash(serialized),
        "inputs_fingerprint": inputs_fingerprint,
        "open_item_ids": [i["item_id"] for i in open_items],
        "article_ids": [a["id"] for a in articles if a.get("id")],
        "scheduled_events": payload["dated_items"]["scheduled_events_this_week"],
        "calendar_rows": calendar_rows,
        "noise": noise,
        "lessons_version": lessons_version,
        "manifest": {
            "blocks": {
                "reading": {"snapshot_run_date": _iso(run_date), "articles": len(articles),
                            "snapshot_fingerprint": snapshot.get("payload_fingerprint")},
                "trend": {"snapshots": len(series), "manifests_read": len(history)},
                "data": {"indicators": len(payload["data"]["indicators"]),
                         "previous_manifest": bool(prev_manifest)},
                "dated_items": {"scheduled_events": len(payload["dated_items"]["scheduled_events_this_week"]),
                                "open_watch_items": len(open_items), "calendar_rows": len(calendar_rows)},
                "memory": {"previous_outlook_run_date": _iso(prev_run_date),
                           "evaluations": len(memory.get("evaluations") or [])},
                "base_rates": {"snapshots_on_record": payload["base_rates"]["snapshots_on_record"],
                               "noise_source": noise.get("source")},
                "calibration_memo": {"lessons_version": lessons_version},
            },
            "calendar": calendar_status,
            "redactions": redactions,
            "noise_floor": noise,
            "as_of": _iso(run_date),
        },
    }


# --- the call ---------------------------------------------------------------


def _system_prompt(run_date: dt.date, noise: Dict[str, Any], variant: str) -> str:
    phrase = noise.get("phrase") or "not yet measured"
    return ai_constants.outlook_prompt_for(run_date, phrase) + OUTLOOK_VARIANTS[variant]["suffix"]


def _build_row(iso2: str, run_date: dt.date, answer: Dict[str, Any], assembled: Dict[str, Any], *,
               variant: str, violations: List[Dict[str, Any]], counts: Dict[str, int]) -> Dict[str, Any]:
    suffix = OUTLOOK_VARIANTS[variant]["suffix"]
    return {
        "country_iso2": iso2,
        "run_date": run_date,
        "horizon_4w": answer["horizon_4w"],
        "horizon_12w": answer["horizon_12w"],
        "ledger_direction": answer["ledger_direction"],
        "drivers": answer["drivers"],
        "base_case": answer["base_case"],
        "upside_risk": answer["upside_risk"],
        "downside_risk": answer["downside_risk"],
        "headed_text": answer["headed_text"],
        "confidence": answer["confidence"],
        "key_uncertainties": answer["key_uncertainties"],
        "changes_since_last": answer["changes_since_last"],
        "outlook_prompt_version": ai_constants.OUTLOOK_PROMPT_VERSION,
        "outlook_model": OUTLOOK_MODEL,
        "git_sha": provenance.git_sha(),
        "payload_fingerprint": assembled["payload_fingerprint"],
        "lessons_version": assembled["lessons_version"],
        "inputs_fingerprint": assembled["inputs_fingerprint"],
        "payload_tokens": assembled["payload_tokens"],
        "manifest": {
            **assembled["manifest"],
            "variant": variant,
            "variant_suffix_hash": content_hash(suffix) if suffix else None,
            "schema_version": ai_constants.OUTLOOK_SCHEMA_VERSION,
            "model": OUTLOOK_MODEL,
            "violations": violations,
            "counts": counts,
            "schema_violations": len(violations),
            "watch": {"resolutions": answer["watch_resolutions"], "new_watch": answer["new_watch"]},
            "payload": assembled["payload"],
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
    }


def _existing(iso2: str, run_date: dt.date, variant: str) -> Optional[Dict[str, Any]]:
    if variant == PRODUCTION_VARIANT:
        return store.read_outlook(iso2, run_date)
    rows = store.read_diagnostics(kind=ARM_KIND, variant=variant, country_iso2=iso2, run_date=run_date)
    return rows[0]["payload"] if rows else None


def _print_row(iso2: str, run_date: dt.date, variant: str, row: Dict[str, Any], tokens: int, cost: float, skipped: bool = False) -> None:
    h4, h12 = row["horizon_4w"], row["horizon_12w"]
    tag = "already written" if skipped else f"{tokens} tok, ${cost:.4f}"
    print(f"[outlook] {iso2} {run_date} {variant}: 4w {h4['direction']} {h4['score_low']}-{h4['score_high']} | "
          f"12w {h12['direction']} {h12['score_low']}-{h12['score_high']} | conf {row['confidence']} | "
          f"violations {(row.get('manifest') or {}).get('schema_violations', 0)} | {tag}")


def run(
    iso2: str, run_date: dt.date, *,
    meter: Optional[usage.Meter] = None,
    variant: str = PRODUCTION_VARIANT,
    force: bool = False,
    structured: Any = None,
) -> Dict[str, Any]:
    """Assemble, call, validate and write one country's outlook for one date.

    The production variant writes `country_outlook`; any other variant writes
    a `snapshot_diagnostic` row of kind `outlook-arm`. An existing row is kept
    unless `force`: a forecast is immutable, and re-running the week is a new
    row only when the week is new.

    Returns:
        ``{"row", "answer", "watch_resolutions", "new_watch", "violations",
        "counts", "failed", "skipped", "scheduled_events", "calendar_rows",
        "payload_tokens", "cost_usd"}``.
    """
    if variant not in OUTLOOK_VARIANTS:
        raise ValueError(f"unknown outlook variant {variant!r}; known: {sorted(OUTLOOK_VARIANTS)}")
    meter = meter or usage.Meter()
    existing = None if force else _existing(iso2, run_date, variant)
    if existing:
        if isinstance(existing.get("run_date"), str):
            existing["run_date"] = _d(existing["run_date"])
        inputs = dated_inputs(iso2, run_date)
        w = (existing.get("manifest") or {}).get("watch") or {}
        _print_row(iso2, run_date, variant, existing, 0, 0.0, skipped=True)
        return {"row": existing, "answer": None, "watch_resolutions": w.get("resolutions") or [],
                "new_watch": w.get("new_watch") or [], "violations": (existing.get("manifest") or {}).get("violations") or [],
                "counts": (existing.get("manifest") or {}).get("counts") or {}, "failed": None, "skipped": True,
                "payload_tokens": existing.get("payload_tokens"), "cost_usd": 0.0, **inputs}

    assembled = assemble_outlook_payload(iso2, run_date)
    spend_before = meter.spend_usd
    result = langchain_llm.structured_call(
        system=_system_prompt(run_date, assembled["noise"], variant),
        user=assembled["serialized"],
        schema=ai_constants.build_outlook_schema(assembled["open_item_ids"]),
        model=OUTLOOK_MODEL, meter=meter, structured=structured,
    )
    cost = meter.spend_usd - spend_before
    if result["failed"]:
        print(f"[outlook] {iso2} {run_date} {variant}: FAILED {result['failed']}")
        return {"row": None, "answer": None, "watch_resolutions": [], "new_watch": [], "violations": [],
                "counts": {}, "failed": result["failed"], "skipped": False,
                "scheduled_events": assembled["scheduled_events"], "calendar_rows": assembled["calendar_rows"],
                "payload_tokens": assembled["payload_tokens"], "cost_usd": cost}

    checked = outlook_validate.validate_outlook(
        result["parsed"], run_date=run_date, open_item_ids=assembled["open_item_ids"],
        article_ids=assembled["article_ids"], iso2=iso2,
    )
    if checked["answer"] is None:
        return {"row": None, "answer": None, "watch_resolutions": [], "new_watch": [],
                "violations": checked["violations"], "counts": checked["counts"],
                "failed": "answer was not an object", "skipped": False,
                "scheduled_events": assembled["scheduled_events"], "calendar_rows": assembled["calendar_rows"],
                "payload_tokens": assembled["payload_tokens"], "cost_usd": cost}

    row = _build_row(iso2, run_date, checked["answer"], assembled, variant=variant,
                     violations=checked["violations"], counts=checked["counts"])
    if variant == PRODUCTION_VARIANT:
        store.write_outlook(row)
    else:
        store.write_diagnostic(country_iso2=iso2, run_date=run_date, kind=ARM_KIND, model=OUTLOOK_MODEL,
                               variant=variant, payload=json.loads(_serialize(row)))
    _print_row(iso2, run_date, variant, row, assembled["payload_tokens"], cost)
    return {"row": row, "answer": checked["answer"],
            "watch_resolutions": checked["answer"]["watch_resolutions"],
            "new_watch": checked["answer"]["new_watch"],
            "violations": checked["violations"], "counts": checked["counts"], "failed": None, "skipped": False,
            "scheduled_events": assembled["scheduled_events"], "calendar_rows": assembled["calendar_rows"],
            "payload_tokens": assembled["payload_tokens"], "cost_usd": cost}


def run_week(iso2: str, run_date: dt.date, *, meter: Optional[usage.Meter] = None,
             arms: bool = True, force: bool = False) -> Dict[str, Any]:
    """The outlook, then the watch list, then the diagnostic arm: one country's
    outlook week, in the order the pipeline and the backfill both use.

    A failed outlook still runs the watch list on the scorer's events and the
    calendar, so the list keeps aging while the forecast is missing.
    """
    result = run(iso2, run_date, meter=meter, force=force)
    watched = watch.run(
        iso2, run_date,
        scheduled_events=result.get("scheduled_events") or [],
        new_watch=result.get("new_watch") or [],
        watch_resolutions=result.get("watch_resolutions") or [],
        calendar_rows=result.get("calendar_rows") or [],
    )
    arm_results = {}
    if arms:
        for name in OUTLOOK_VARIANTS:
            if name != PRODUCTION_VARIANT:
                arm_results[name] = run(iso2, run_date, meter=meter, variant=name, force=force)
    return {"outlook": result, "watch": watched, "arms": arm_results}


# --- the backfill -----------------------------------------------------------


def _stored_pairs(countries: Sequence[str]) -> List[Tuple[dt.date, str]]:
    rows = store.read_roster_scores()
    pairs = sorted({(r["run_date"], r["country_iso2"]) for r in rows if r["country_iso2"] in countries})
    order = {c: i for i, c in enumerate(countries)}
    return sorted(pairs, key=lambda p: (p[0], order[p[1]]))


def backfill(
    countries: Optional[Sequence[str]] = None, *,
    cap_usd: float = 8.0, dry_run: bool = False, limit: Optional[int] = None,
    arms: bool = True, force: bool = False,
) -> Dict[str, Any]:
    """Outlooks for every stored week of the test set, oldest first, without
    leaking the future: each is assembled from rows dated on or before its own
    date, and the watch list is run for that week so carried items build up as
    they would have live. Then the ledger is evaluated and the report printed.

    The cost is measured before anything is paid for: the largest payload is
    counted and projected over every call, and the run stops above `cap_usd`.
    """
    db.announce()
    countries = list(countries or constants.TEST_SET)
    loaded = forecast_eval.load_step5e_repeats()
    print(f"[backfill] repeat rows available for the noise floor: {loaded}")
    pairs = _stored_pairs(countries)
    if not pairs:
        print("[backfill] no stored snapshots for the test set; nothing to do")
        return {"pairs": 0}
    dates = sorted({d for d, _ in pairs})
    print(f"[backfill] {len(pairs)} country-weeks over {len(dates)} run dates: "
          f"{', '.join(d.isoformat() for d in dates)}")

    # Measure before projecting: the first and last week of every country when
    # dry, otherwise one country-week from each end of the record.
    sample = pairs if dry_run else sorted({pairs[0], *[p for p in pairs if p[0] == dates[-1]]})
    measured: List[Tuple[dt.date, str, int]] = []
    for d, c in sample:
        try:
            a = assemble_outlook_payload(c, d)
            measured.append((d, c, a["payload_tokens"]))
            print(f"[measure] {c} {d}: {a['payload_tokens']} payload tokens, "
                  f"{len(a['open_item_ids'])} open items, calendar {a['manifest']['calendar']}, "
                  f"{len(a['manifest']['redactions'])} redacted strings")
        except gazetteer.MaskLeak as exc:
            print(f"[measure] {c} {d}: MASK LEAK {exc}")
    if not measured:
        raise RuntimeError("no country-week could be assembled")
    from backend.llm.payload import count_tokens
    prompt_tokens = int(count_tokens(_system_prompt(dates[-1], forecast_eval.noise_floor(), PRODUCTION_VARIANT))["tokens"]) + 50
    per_call = max(t for _, _, t in measured) + prompt_tokens
    calls = len(pairs) * (len(OUTLOOK_VARIANTS) if arms else 1)
    projected = usage.project("outlook backfill", model=OUTLOOK_MODEL, calls=calls,
                              input_tokens_per_call=per_call, output_tokens_per_call=OUTPUT_TOKENS_PER_CALL,
                              cap_usd=cap_usd)
    if dry_run:
        return {"pairs": len(pairs), "measured": measured, "projected_usd": projected}

    meter = usage.Meter()
    started = dt.datetime.now(dt.timezone.utc)
    outcomes: List[Dict[str, Any]] = []
    done = 0
    for d, c in pairs:
        t0 = time.monotonic()
        try:
            week = run_week(c, d, meter=meter, arms=arms, force=force)
            res = week["outlook"]
            outcomes.append({"iso2": c, "run_date": d.isoformat(), "ok": res["failed"] is None,
                             "skipped": res["skipped"], "violations": len(res["violations"]),
                             "tokens": res["payload_tokens"], "usd": round(res["cost_usd"], 4),
                             "seconds": round(time.monotonic() - t0, 1)})
        except gazetteer.MaskLeak as exc:
            print(f"[backfill] {c} {d}: MASK LEAK, skipped: {exc}")
            outcomes.append({"iso2": c, "run_date": d.isoformat(), "ok": False, "error": str(exc)[:300]})
        except Exception as exc:  # one week's failure must not stop the record from building
            print(f"[backfill] {c} {d}: ERROR {exc}")
            outcomes.append({"iso2": c, "run_date": d.isoformat(), "ok": False, "error": str(exc)[:300]})
        meter.check(cap_usd)
        done += 1
        if limit and done >= limit:
            print(f"[backfill] stopping after {done} country-weeks (--limit)")
            break

    evaluated = forecast_eval.run(dates[-1])
    forecast_eval.report()
    print(meter.summary())
    paid = [o for o in outcomes if o.get("ok") and not o.get("skipped")]
    if paid:
        print(f"[backfill] mean cost per outlook call ${sum(o['usd'] for o in paid) / len(paid):.4f} "
              f"over {len(paid)} paid production calls; projected ${projected:.4f} for {calls} calls")
    try:
        store.write_ledger(job_type="outlook-backfill", run_date=dates[-1], status="ok",
                           started_at=started, finished_at=dt.datetime.now(dt.timezone.utc),
                           git_sha=provenance.git_sha(), input_tokens=meter.input_tokens,
                           output_tokens=meter.output_tokens, spend_usd=round(meter.spend_usd, 6),
                           detail={"pairs": len(pairs), "outcomes": outcomes, "evaluated": evaluated,
                                   "projected_usd": round(projected, 4)})
    except Exception as exc:
        print(f"[backfill] ledger not recorded: {exc}")
    return {"pairs": len(pairs), "outcomes": outcomes, "spend_usd": meter.spend_usd, "evaluated": evaluated}


# --- the identifiability probe ---------------------------------------------


def _same_country(guess: str, name: str, iso2: str) -> bool:
    """Does a probe's guess name the subject? The step 5e rule."""
    g = guess.strip().lower()
    terms = {t.lower() for t, k in gazetteer.terms_for(iso2) if k in ("name", "alias", "code")}
    return g == name.lower() or g in terms or any(t in g for t in terms if len(t) > 3)


def probe(iso2: str, run_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """Which country is this? The step 5e probe, on an assembled outlook payload.

    The verdict is recorded in the doc, never in the database: it measures the
    masking, not the country.
    """
    run_date = run_date or max(r["run_date"] for r in store.read_snapshot_series(iso2))
    assembled = assemble_outlook_payload(iso2, run_date)
    meter = usage.Meter()
    result = langchain_llm.structured_call(
        system=ai_constants.PROBE_PROMPT, user=assembled["serialized"], schema=ai_constants.PROBE_SCHEMA,
        model=ai_constants.PROBE_MODEL, meter=meter,
    )
    if result["failed"]:
        print(f"[probe] {iso2} {run_date}: FAILED {result['failed']}")
        return {"iso2": iso2, "run_date": run_date.isoformat(), "verdict": "failed", "error": result["failed"]}
    parsed = result["parsed"] or {}
    name = constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2)
    guesses = sorted(parsed.get("guesses") or [], key=lambda g: -g.get("probability", 0))
    top = guesses[0] if guesses else None
    correct = bool(top) and _same_country(top["country"], name, iso2)
    verdict = ("not enough to tell" if not parsed.get("enough_to_tell") or not top
               else "identified" if correct and top["probability"] >= 0.5
               else "wrong" if not correct else "identified (under 0.5)")
    reason = (top or {}).get("reason", "")
    print(f"[probe] {iso2} {run_date}: {verdict}; top guess {top and top['country']} "
          f"p={top and top['probability']}; reason: {reason[:160]}; ${meter.spend_usd:.4f}")
    return {"iso2": iso2, "run_date": run_date.isoformat(), "verdict": verdict, "top": top,
            "guesses": guesses, "reason": reason, "payload_tokens": assembled["payload_tokens"],
            "spend_usd": round(meter.spend_usd, 4)}


# --- entry point ------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> None:
    from backend.util import env

    env.load()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="outlooks for every stored week of the test set, oldest first")
    b.add_argument("--dry-run", action="store_true", help="assemble and project the cost; call nothing")
    b.add_argument("--limit", type=int, default=None, help="stop after N country-weeks")
    b.add_argument("--no-arms", action="store_true", help="skip the diagnostic prompt variants")
    b.add_argument("--force", action="store_true", help="rewrite weeks that already have an outlook")
    b.add_argument("--cap", type=float, default=8.0, help="spend cap in USD")
    b.add_argument("--countries", default="", help="comma-separated iso2 list, default the test set")
    a = sub.add_parser("arm", help="run one prompt variant for one country-week into snapshot_diagnostic")
    a.add_argument("--variant", required=True, choices=sorted(OUTLOOK_VARIANTS))
    a.add_argument("--country", required=True)
    a.add_argument("--date", default="")
    p = sub.add_parser("probe", help="the identifiability probe on an assembled outlook payload")
    p.add_argument("--country", required=True)
    p.add_argument("--date", default="")
    args = ap.parse_args(argv)

    if args.cmd == "backfill":
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()] or None
        backfill(countries, cap_usd=args.cap, dry_run=args.dry_run, limit=args.limit,
                 arms=not args.no_arms, force=args.force)
    elif args.cmd == "arm":
        db.announce()
        when = dt.date.fromisoformat(args.date) if args.date else max(
            r["run_date"] for r in store.read_snapshot_series(args.country.upper()))
        run(args.country.upper(), when, variant=args.variant)
    elif args.cmd == "probe":
        db.announce()
        probe(args.country.upper(), dt.date.fromisoformat(args.date) if args.date else None)


if __name__ == "__main__":
    main()
