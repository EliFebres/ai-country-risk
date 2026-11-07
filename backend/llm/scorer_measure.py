"""
Measure the scorer on frozen payloads (step 5, and step 5b on the same files).

The payload is fixed first and the scorer is measured second. `freeze` builds
this week's payload for each country through the normal pipeline
(`pipeline.assemble_country`) and writes it to a file with its fingerprint.
Every scoring call after that reads the file. Nothing is re-fetched, so a change
in output comes from the scorer and not from the input.

Steps 5b and 5c read the step 5 payloads from `backend/measurements/step5/`
and write everything new under their own directory, so step 5's record stays
as it was. The data half (`data`) is computed from the local panel and curated
CSV with no model call, printed, checked and saved beside the calls.

Nothing here writes `risk_snapshot` or `run_ledger`. Each call's answer goes to
a JSON-lines file under `calls/`, one file per arm and country, and a rerun
appends to it, so an interrupted arm resumes where it stopped.

    python -m backend.llm.scorer_measure data
    python -m backend.llm.scorer_measure score --arm full --n 15 --cap-usd 4
    python -m backend.llm.scorer_measure score --arm first_5 --arm first_10 --arm first_15 --n 3 --cap-usd 3
    python -m backend.llm.scorer_measure analyse

Production mode is the per-question median of three calls, so every three
consecutive calls of an arm are one result. The arms:

- `full`: the frozen payload as production would send it now (no economics
  block). Fifteen calls are five medians.
- `first_5`, `first_10`, `first_15`: the first n selected articles, in
  selection order, with the counts and coverage recomputed for the subset.
  Three calls are one median; `full`'s first median is the "all" point.

It prints counts. Titles and verdicts stay in the files.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import statistics as st
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.util import env
env.load()

from backend.data_fetching import curated_loader, data_retrieval
from backend.llm import combine, data_score
from backend.llm import constants as ai_constants
from backend.llm import langchain_llm, payload_health, relevance
from backend.llm import payload as payload_builder
from backend.llm.payload import count_tokens
from backend.util import constants, paths, pipeline, usage

__all__ = ["freeze", "ARMS", "score_arm", "analyse"]

FROZEN_DIR = paths.PROJECT_ROOT / "backend" / "measurements" / "step5"
OUT_DIR = paths.PROJECT_ROOT / "backend" / "measurements" / "step5c"
CALLS_DIR = OUT_DIR / "calls"
DATA_PATH = OUT_DIR / "data_table.json"

SCORING_MODEL = pipeline.SCORING_MODEL
SEED = 42

def _payload_path(iso2: str) -> Path:
    own = OUT_DIR / f"payload_{iso2}.json"
    return own if own.exists() else FROZEN_DIR / f"payload_{iso2}.json"


def _frozen_countries() -> List[str]:
    seen = {p.stem.split("_", 1)[1] for d in (FROZEN_DIR, OUT_DIR) for p in d.glob("payload_*.json")}
    return sorted(seen)


def _sent_bytes(payload: Dict[str, Any]) -> str:
    """The user message exactly as `score_country` serializes it."""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Freeze -------------------------------------------------------------------


def freeze(countries: List[str], cap_usd: float) -> Dict[str, Any]:
    """Build and save each country's payload. Returns the dedup report."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meter = usage.Meter()
    run_as_of = dt.datetime.now(dt.timezone.utc).date()
    report: Dict[str, Any] = {"run_date": run_as_of.isoformat(), "countries": {}}

    for iso2 in countries:
        name = constants.COUNTRY_NAME_BY_ISO2[iso2]
        built = pipeline.assemble_country(name, iso2, run_as_of, meter)
        meter.check(cap_usd)

        gate, stories = built["gate"], built["stories"]
        # What selection would have taken without the story step, to count the
        # slots it freed: selected articles that are another outlet's telling.
        undeduped = relevance.select(built["candidates"], built["labels"], iso2)
        dropped = set(stories["same_story_as"])
        slots_freed = sum(
            1 for a in undeduped["selected"]
            if (a.get("publisher_link") or a.get("link")) in dropped
        )

        payload = built["scoring_payload"]
        by_url = {r["url"]: r for r in gate["gate_labels"]}
        meta = []
        for it in built["items"]:
            url = it.get("publisher_link") or it.get("link")
            verdict = by_url.get(url) or {}
            meta.append({
                "id": it.get("id"),
                "url": url,
                "title": it.get("title"),
                "publisher": it.get("source"),
                "published": (it.get("page_published_at") or it.get("published") or "")[:10],
                "test": verdict.get("test"),
                "exclusion": verdict.get("exclusion"),
                "ledger": verdict.get("ledger"),
                "high_impact_event": verdict.get("high_impact_event"),
                "what_happened": verdict.get("what_happened"),
                "body_quality": it.get("body_quality"),
                "body_status": it.get("body_status"),
            })

        dedup = {
            "method": stories["method"],
            "problem": stories["problem"],
            **stories["counts"],
            "slots_freed": slots_freed,
            "selected": len(gate["selected"]),
            "same_story_as": stories["same_story_as"],
        }
        sent = _sent_bytes(payload)
        frozen = {
            "iso2": iso2,
            "country": name,
            "run_date": run_as_of.isoformat(),
            "payload_fingerprint": built["census"]["payload_fingerprint"],
            "payload_sha256": _sha(sent),
            "payload_tokens": built["payload_tokens"],
            "versions": built["census"]["versions"],
            "article_ids": built["article_ids"],
            "articles_meta": meta,
            "economics_resolution": built["econ"]["resolution"],
            "dedup": dedup,
            "payload": payload,
        }
        path = OUT_DIR / f"payload_{iso2}.json"
        path.write_text(
            json.dumps(frozen, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
        )
        # Reading it back must give the same bytes to the scorer.
        again = json.loads(path.read_text(encoding="utf-8"))
        assert _sha(_sent_bytes(again["payload"])) == frozen["payload_sha256"], iso2

        report["countries"][iso2] = {k: v for k, v in dedup.items() if k != "same_story_as"}
        print(f"[freeze] {iso2}: {dedup['eligible']} eligible, {dedup['multi']} multi-article "
              f"stories, {dedup['dropped']} set aside, {slots_freed} slots freed "
              f"({dedup['method']}); {len(built['article_ids'])} selected, "
              f"{built['payload_tokens']} payload tokens")

    report["spend_usd"] = meter.spend_usd
    print(meter.summary())
    (OUT_DIR / "part1_dedup.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


# --- The data half --------------------------------------------------------------


def build_data_table(countries: List[str]) -> Dict[str, Any]:
    """The data half for each frozen country, from the panel, with no model call.

    The economics block is rebuilt from the local panel and `curated.csv` at the
    frozen run date, because the frozen files hold only the codes the registry
    had in September and the scorer no longer reads the block anyway. Prints
    every scored value and runs the brief's check: RU highest on data, PT
    lowest or second lowest.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    table: Dict[str, Any] = {}
    for iso2 in countries:
        frozen = load_frozen(iso2)
        run_date = dt.date.fromisoformat(frozen["run_date"])
        econ = payload_builder.build_economics_block(
            data_retrieval.panel_values(iso2, lookback=10),
            curated_loader.load_for_country(iso2), today=run_date,
        )
        data = data_score.ledger_data_scores(econ["ledgers"])
        table[iso2] = {"run_date": frozen["run_date"], "ledgers": econ["ledgers"],
                       "resolution": econ["resolution"], "data": data}
        print(f"== {iso2}  data scores {data['data_scores']}  "
              f"indicators by ledger {data['n_by_ledger']}  unscored {data['unscored']}")
        for code, v in data["indicators"].items():
            print(f"   {v['ledger']:<11} {code:<22} {v['value']:>9.3f} ({v['period']}) -> "
                  f"{v['score']:>5.1f}{' clamped' if v['clamped'] else ''}")

    def composite(ds: Dict[str, Optional[float]]) -> Optional[float]:
        w = {k: v for k, v in ai_constants.SCORING["composite_weights"].items() if v > 0}
        present = {k: ds[k] for k in w if ds.get(k) is not None}
        if not present:
            return None
        t = sum(w[k] for k in present)
        return round(sum(w[k] / t * present[k] for k in present), 1)

    comp = {c: composite(table[c]["data"]["data_scores"]) for c in countries}
    order = sorted((c for c in comp if comp[c] is not None), key=lambda c: comp[c], reverse=True)
    check = {
        "data_only_composite": {c: comp[c] for c in order},
        "ru_highest": bool(order) and order[0] == "RU",
        "pt_rank_from_bottom": (len(order) - order.index("PT")) if "PT" in order else None,
    }
    check["pass"] = bool(check["ru_highest"] and check["pt_rank_from_bottom"] in (1, 2))
    print(f"[data] data-only composite: {[(c, comp[c]) for c in order]}; "
          f"RU highest {check['ru_highest']}, PT {check['pt_rank_from_bottom']} from bottom: "
          f"{'PASS' if check['pass'] else 'FAIL'}")
    DATA_PATH.write_text(json.dumps({"countries": table, "check": check}, indent=1, default=str),
                         encoding="utf-8")
    return {"countries": table, "check": check}


def load_data_table() -> Dict[str, Any]:
    if not DATA_PATH.exists():
        raise RuntimeError(
            f"{DATA_PATH} is missing; run `python -m backend.llm.scorer_measure data` first"
        )
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))["countries"]


# --- Arms -------------------------------------------------------------------


def _set_components(p: Dict[str, Any], **changes: float) -> None:
    comps = p["evidence_coverage_components"]["components"]
    comps.update({k: round(v, 4) for k, v in changes.items()})


def _as_production(frozen: Dict[str, Any]) -> Dict[str, Any]:
    """The frozen payload as `build_scoring_payload` would send it now: no economics block."""
    p = copy.deepcopy(frozen["payload"])
    p.pop("economics_by_ledger", None)
    return p


def arm_full(frozen: Dict[str, Any]) -> Dict[str, Any]:
    return _as_production(frozen)


def _first(n: int) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def arm(frozen: Dict[str, Any]) -> Dict[str, Any]:
        p = _as_production(frozen)
        keep = p["articles"][:n]
        ids = {a["id"] for a in keep}
        p["articles"] = keep
        p["full_texts"] = [f for f in p["full_texts"] if f["id"] in ids]
        cov = p["article_coverage"]
        cov["selected_per_theme"] = {
            t: sum(1 for a in keep if t in (a.get("themes") or []))
            for t in cov["selected_per_theme"]
        }
        cov["selected_per_ledger"] = {
            led: sum(1 for a in keep if led in (a.get("ledgers") or []))
            for led in cov["selected_per_ledger"]
        }
        cov["selected"] = len(keep)
        budget = cov.get("budget") or 20
        depth = (sum(payload_health.BODY_DEPTH.get(a.get("body_status", "title-only"), 0.1)
                     for a in keep) / len(keep)) if keep else 0.0
        _set_components(p, article_volume=min(len(keep) / budget, 1.0), body_depth=depth)
        return p
    return arm


ARMS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "full": arm_full,
    "first_5": _first(5),
    "first_10": _first(10),
    "first_15": _first(15),
}

K = 3  # production mode: the per-question median of three calls


# --- Scoring ----------------------------------------------------------------


def _calls_path(arm: str, iso2: str) -> Path:
    return CALLS_DIR / f"{arm}_{iso2}.jsonl"


def read_calls(arm: str, iso2: str) -> List[Dict[str, Any]]:
    path = _calls_path(arm, iso2)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_frozen(iso2: str) -> Dict[str, Any]:
    frozen = json.loads(_payload_path(iso2).read_text(encoding="utf-8"))
    if _sha(_sent_bytes(frozen["payload"])) != frozen["payload_sha256"]:
        raise RuntimeError(f"{iso2}: the frozen payload no longer matches its fingerprint")
    return frozen


def _score(frozen: Dict[str, Any], payload: Dict[str, Any], answer: Dict[str, Any],
           data: Dict[str, Any], validation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return combine.combine(
        answer=answer, data=data,
        article_ids=[a["id"] for a in payload["articles"]],
        run_date=dt.date.fromisoformat(frozen["run_date"]),
        history=None, validation=validation,
    )


def project(arms: Dict[str, int], countries: List[str]) -> float:
    """Projected cost of the remaining calls, from the payloads' real sizes."""
    total = 0.0
    for iso2 in countries:
        frozen = load_frozen(iso2)
        prompt_tokens = count_tokens(ai_constants.prompt_for(frozen["country"]))["tokens"]
        for arm, n in arms.items():
            todo = max(n - len(read_calls(arm, iso2)), 0)
            if not todo:
                continue
            p = ARMS[arm](frozen)
            tokens_in = prompt_tokens + count_tokens(_sent_bytes(p))["tokens"] + 50
            tokens_out = 900 + 10 * len(p["articles"])
            total += usage.cost_usd(SCORING_MODEL, todo * tokens_in, todo * tokens_out)
    return total


def score_arm(arm: str, n: int, countries: List[str], meter: usage.Meter, cap_usd: float) -> None:
    CALLS_DIR.mkdir(parents=True, exist_ok=True)
    table = load_data_table()
    for iso2 in countries:
        frozen = load_frozen(iso2)
        payload = ARMS[arm](frozen)
        ids = [a["id"] for a in payload["articles"]]
        sent_sha = _sha(_sent_bytes(payload))
        data = table[iso2]["data"]
        done = len(read_calls(arm, iso2))
        for i in range(done, n):
            before = (meter.input_tokens, meter.output_tokens)
            # The production call: the same model, temperature, seed, prompt
            # and schema that `run_etl` uses.
            scored = langchain_llm.score_country(
                iso2=iso2, payload=payload, article_ids=ids,
                model=SCORING_MODEL, seed=SEED, meter=meter,
                run_date=dt.date.fromisoformat(frozen["run_date"]),
            )
            scoring = None
            if scored["answer"]:
                try:
                    scoring = _score(frozen, payload, scored["answer"], data,
                                     {"violations": scored["violations"], "counts": scored["counts"]})
                except ValueError as exc:
                    scored["failed"] = f"combine: {exc}"
            row = {
                "arm": arm, "iso2": iso2, "i": i,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "payload_sha256": sent_sha,
                "prompt_version": ai_constants.PROMPT_VERSION,
                "failed": scored["failed"],
                "answer": scored["answer"],
                "raw": scored["raw"],
                "violations": scored["violations"],
                "counts": scored.get("counts"),
                "scoring": scoring,
                "input_tokens": meter.input_tokens - before[0],
                "output_tokens": meter.output_tokens - before[1],
            }
            with _calls_path(arm, iso2).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            s = scoring or {}
            print(f"[score] {arm} {iso2} #{i + 1}: 12m={s.get('score_12m')} 3m={s.get('score_3m')} "
                  f"ledgers={s.get('ledger_ints')} "
                  f"({'failed: ' + scored['failed'][:60] if scored['failed'] else 'ok'}) "
                  f"${meter.spend_usd:.3f} so far")
            meter.check(cap_usd)


# --- Analysis ---------------------------------------------------------------

LEDGER_SCORES = ("friction", "order", "information", "edge")


def _stats(values: List[float]) -> Dict[str, Any]:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "distinct": sorted(set(vals)),
        "range": round(max(vals) - min(vals), 2),
        "sd": round(st.stdev(vals), 3) if len(vals) > 1 else 0.0,
        "median": st.median(vals),
        "mean": round(st.mean(vals), 2),
    }


def _scored(arm: str, iso2: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = [r for r in read_calls(arm, iso2) if r.get("scoring")]
    return rows[:limit] if limit else rows


def _slope(points: List[tuple]) -> Optional[float]:
    if len(points) < 2:
        return None
    mx = sum(x for x, _ in points) / len(points)
    my = sum(y for _, y in points) / len(points)
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if not sxx:
        return None
    return sum((x - mx) * (y - my) for x, y in points) / sxx


def _levels(scoring: Dict[str, Any]) -> Dict[str, Optional[int]]:
    return {q: v.get("level") for q, v in scoring["questions"].items()}


def _question_agreement(scorings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per question: the modal level, how many results gave it, and the null share."""
    out = {}
    for qid in ai_constants.QUESTION_IDS:
        levels = [_levels(s).get(qid) for s in scorings]
        c = Counter(levels)
        modal, count = c.most_common(1)[0] if c else (None, 0)
        out[qid] = {
            "levels": levels,
            "modal": modal,
            "modal_share": round(count / len(levels), 2) if levels else None,
            "null_share": round(sum(v is None for v in levels) / len(levels), 2) if levels else None,
            "flips": len(levels) - count,
        }
    return out


def _medians(arm: str, iso2: str, table: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every K consecutive calls of an arm merged by per-question median, then scored."""
    rows = _scored(arm, iso2)
    frozen = load_frozen(iso2)
    payload = ARMS[arm](frozen)
    out = []
    for k in range(0, len(rows) - len(rows) % K, K):
        merged = combine.merge_answers([r["answer"] for r in rows[k:k + K]])
        out.append(_score(frozen, payload, merged, table[iso2]["data"]))
    return out


def analyse() -> Dict[str, Any]:
    """M1 in median-of-3 mode, face validity, M3, M4, M5, dilution, cost. Writes analysis.json."""
    countries = [c for c in _frozen_countries() if _scored("full", c)]
    table = load_data_table()
    data_check = json.loads(DATA_PATH.read_text(encoding="utf-8"))["check"]
    out: Dict[str, Any] = {"countries": countries, "prompt_version": ai_constants.PROMPT_VERSION,
                           "data_check": data_check}

    # M1: repeatability of the production result, the median of three.
    m1: Dict[str, Any] = {}
    medians_by: Dict[str, List[Dict[str, Any]]] = {}
    for iso2 in countries:
        singles = [r["scoring"] for r in _scored("full", iso2)]
        meds = _medians("full", iso2, table)
        medians_by[iso2] = meds
        counts = Counter()
        for r in _scored("full", iso2):
            counts.update(r.get("counts") or {})
        m1[iso2] = {
            "calls": len(singles),
            "failed": len(read_calls("full", iso2)) - len(singles),
            "medians": len(meds),
            "score_12m": _stats([m["score_12m"] for m in meds]),
            "score_3m": _stats([m["score_3m"] for m in meds]),
            **{f: _stats([m["ledger_ints"].get(f) for m in meds]) for f in LEDGER_SCORES},
            "single_call_score_12m": _stats([s["score_12m"] for s in singles]),
            "questions": _question_agreement(meds),
            "questions_flipping": [q for q, a in _question_agreement(meds).items() if a["flips"]],
            "questions_flipping_single_calls": [q for q, a in _question_agreement(singles).items() if a["flips"]],
            "validator_counts": dict(counts),
            "zero_with_no_evidence": sum(
                1 for r in _scored("full", iso2) for q in ai_constants.QUESTION_IDS
                if ((r.get("raw") or {}).get(q) or {}).get("level") == 0
                and not ((r.get("raw") or {}).get(q) or {}).get("evidence")),
        }
    worst = max((m1[c]["score_12m"].get("sd", 0) for c in countries), default=0)
    out["m1"] = {"per_country": m1, "worst_sd_12m_median_of_3": worst,
                 "worst_sd_12m_single_call": max((m1[c]["single_call_score_12m"].get("sd", 0) for c in countries), default=0),
                 "branch": "pass" if worst <= 1 else "stop"}

    # Face validity on the median result.
    meds12 = {c: m1[c]["score_12m"].get("median") for c in countries if m1[c]["score_12m"].get("n")}
    order = sorted(meds12, key=lambda c: meds12[c], reverse=True)
    fv: Dict[str, Any] = {"order": [(c, meds12[c]) for c in order]}
    if meds12:
        fv["ru_highest"] = order[0] == "RU"
        fv["pt_rank_from_bottom"] = (len(order) - order.index("PT")) if "PT" in order else None
        fv["pass"] = bool(fv["ru_highest"] and fv["pt_rank_from_bottom"] in (1, 2))
        fv["ledger_medians"] = {c: {f: m1[c][f].get("median") for f in LEDGER_SCORES} for c in countries}
        fv["branch"] = "pass" if fv["pass"] else "fail"
    out["face_validity"] = fv

    # M3: the full score against the data-only score (every item at the data level).
    m3: Dict[str, Any] = {}
    for iso2 in countries:
        frozen = load_frozen(iso2)
        payload = arm_full(frozen)
        empty = {"questions": {q: {"level": None, "evidence": [], "note": ""} for q in ai_constants.QUESTION_IDS},
                 "scheduled_events": [], "condition_flags": {}, "bullet_summary": ""}
        data_only = _score(frozen, payload, empty, table[iso2]["data"])
        full = meds12.get(iso2)
        meds = medians_by[iso2]
        by_q = {}
        for q in ai_constants.QUESTION_IDS:
            lv = [m["questions"][q]["level"] for m in meds]
            answered = [v for v in lv if v is not None]
            if answered:
                ledger = ai_constants.LEDGER_OF_QUESTION[q]
                d = table[iso2]["data"]["data_scores"].get(ledger)
                by_q[q] = {"modal_level": Counter(answered).most_common(1)[0][0],
                           "item": 25 * st.median(answered),
                           "data_level": d,
                           "pull": round(25 * st.median(answered) - d, 1) if d is not None else None}
        m3[iso2] = {
            "data_only_12m": data_only["score_12m"],
            "data_only_ledgers": data_only["ledger_ints"],
            "full_median_12m": full,
            "gap": (full - data_only["score_12m"]) if full is not None else None,
            "questions": by_q,
        }
    gaps = [m3[c]["gap"] for c in countries if m3[c]["gap"] is not None]
    out["m3"] = {"per_country": m3,
                 "mean_abs_gap": round(sum(abs(g) for g in gaps) / len(gaps), 2) if gaps else None,
                 "mean_gap": round(sum(gaps) / len(gaps), 2) if gaps else None}
    if gaps:
        out["m3"]["branch"] = f"articles move the score by {out['m3']['mean_abs_gap']} on average (range {min(gaps)}..{max(gaps)})"

    # M4: the 3-month score, on every single call and every median.
    gaps4: List[Dict[str, Any]] = []
    dropped_dates = 0
    for iso2 in countries:
        for r in _scored("full", iso2):
            s = r["scoring"]
            dropped_dates += (r.get("counts") or {}).get("events_dropped_date_not_in_article", 0)
            gaps4.append({"iso2": iso2, "kind": "call", "i": r["i"], "gap": s["score_12m"] - s["score_3m"],
                          "events": [{k: e.get(k) for k in ("article_id", "date", "question", "direction", "event")}
                                     for e in s["events"]["valid"]]})
        for k, m in enumerate(medians_by[iso2]):
            gaps4.append({"iso2": iso2, "kind": "median", "i": k, "gap": m["score_12m"] - m["score_3m"],
                          "events": [{kk: e.get(kk) for kk in ("article_id", "date", "question", "direction", "event")}
                                     for e in m["events"]["valid"]]})
    dist = Counter(g["gap"] for g in gaps4 if g["kind"] == "call")
    out["m4"] = {
        "calls": sum(1 for g in gaps4 if g["kind"] == "call"),
        "distribution_calls": dict(sorted(dist.items())),
        "distribution_medians": dict(sorted(Counter(g["gap"] for g in gaps4 if g["kind"] == "median").items())),
        "non_zero": [g for g in gaps4 if g["gap"]],
        "all_traced": all(g["events"] for g in gaps4 if g["gap"]),
        "events_dropped_date_not_in_article": dropped_dates,
        "events_valid_total": sum(len(g["events"]) for g in gaps4 if g["kind"] == "call"),
    }
    nz = sum(1 for g in gaps4 if g["gap"] and g["kind"] == "call")
    out["m4"]["branch"] = (f"{nz} of {out['m4']['calls']} calls have a gap; "
                           + ("every one traces to an event whose date is in its article" if out["m4"]["all_traced"]
                              else "A GAP WITH NO EVENT")
                           + f"; {dropped_dates} events dropped for a date not in the article")

    # M5: article count, one median-of-3 per subset; "all" is the first median of `full`.
    per_country: Dict[str, Any] = {}
    pooled_fe: List[tuple] = []
    pooled_raw: List[tuple] = []
    levels_by_count: Dict[int, List[Dict[str, Optional[int]]]] = {}
    for iso2 in countries:
        n_all = len(load_frozen(iso2)["article_ids"])
        pts: List[tuple] = []
        for arm, n in (("first_5", 5), ("first_10", 10), ("first_15", 15), ("full", n_all)):
            meds = medians_by[iso2][:1] if arm == "full" else _medians(arm, iso2, table)
            for m in meds[:1]:
                pts.append((min(n, n_all), m["score_12m"]))
                levels_by_count.setdefault(min(n, n_all), []).append(_levels(m))
        if len({x for x, _ in pts}) < 2:
            continue
        my = sum(y for _, y in pts) / len(pts)
        mx = sum(x for x, _ in pts) / len(pts)
        pooled_fe += [(x - mx, y - my) for x, y in pts]
        pooled_raw += pts
        sl = _slope(pts)
        per_country[iso2] = {"score_by_count": dict(pts), "slope": round(sl, 3) if sl is not None else None}
    pooled = _slope(pooled_fe)
    per_question: Dict[str, Any] = {}
    for qid in ai_constants.QUESTION_IDS:
        per_question[qid] = {
            str(x): {
                "non_null_share": round(sum(l.get(qid) is not None for l in ls) / len(ls), 2),
                "mean_level": (round(st.mean([l[qid] for l in ls if l.get(qid) is not None]), 2)
                               if any(l.get(qid) is not None for l in ls) else None),
            }
            for x, ls in sorted(levels_by_count.items())
        }
    changing = [q for q, by in per_question.items()
                if len({v["non_null_share"] > 0 for v in by.values()}) > 1
                or len({v["mean_level"] for v in by.values() if v["mean_level"] is not None}) > 1]
    out["m5"] = {
        "per_country": per_country,
        "pooled_slope_within_country": round(pooled, 3) if pooled is not None else None,
        "pooled_slope_naive": (round(_slope(pooled_raw), 3) if _slope(pooled_raw) is not None else None),
        "per_question_by_count": per_question,
        "questions_changing_with_count": changing,
    }
    if pooled is not None:
        out["m5"]["branch"] = ("volume question closed" if abs(pooled) <= 0.2
                               else "volume moves the score")

    # Dilution: article weight against the gate's test code, and uncited share.
    by_test: Dict[str, List[int]] = {}
    uncited: List[float] = []
    for iso2 in countries:
        test_of = {m["id"]: m.get("test") for m in load_frozen(iso2)["articles_meta"]}
        for arm in ARMS:
            for r in _scored(arm, iso2):
                weights = r["scoring"]["article_weights"]
                if weights:
                    uncited.append(sum(w["weight"] == 0 for w in weights) / len(weights))
                for w in weights:
                    by_test.setdefault(test_of.get(w["id"]) or "?", []).append(w["weight"])
    out["dilution"] = {
        "by_test": {
            t: {"appearances": len(v), "mean_weight": round(sum(v) / len(v), 2),
                "share_zero": round(sum(b == 0 for b in v) / len(v), 3)}
            for t, v in sorted(by_test.items())
        },
        "uncited_share_mean": round(sum(uncited) / len(uncited), 3) if uncited else None,
    }

    # Spend, metered from the call rows, and the production projection.
    spend: Dict[str, Any] = {}
    for arm in ARMS:
        rows = [r for c in countries for r in read_calls(arm, c)]
        if rows:
            tin = sum(r.get("input_tokens") or 0 for r in rows)
            tout = sum(r.get("output_tokens") or 0 for r in rows)
            spend[arm] = {"calls": len(rows), "input_tokens": tin, "output_tokens": tout,
                          "usd": round(usage.cost_usd(SCORING_MODEL, tin, tout), 3)}
    spend["total_usd"] = round(sum(v["usd"] for v in spend.values()), 3)
    full_rows = [r for c in countries for r in read_calls("full", c)]
    if full_rows:
        per_call = spend["full"]["usd"] / len(full_rows)
        spend["per_call_usd"] = round(per_call, 4)
        spend["per_country_week_median_of_3_usd"] = round(K * per_call, 4)
        spend["weekly_48_countries_usd"] = round(48 * K * per_call, 2)
    out["spend"] = spend

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "analysis.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("countries", nargs="+")
    f.add_argument("--cap-usd", type=float, default=1.5)
    d = sub.add_parser("data")
    d.add_argument("--countries", nargs="+", default=None)
    s = sub.add_parser("score")
    s.add_argument("--arm", action="append", required=True, choices=sorted(ARMS))
    s.add_argument("--n", type=int, required=True)
    s.add_argument("--countries", nargs="+", default=None)
    s.add_argument("--cap-usd", type=float, required=True,
                   help="cap on this invocation's metered spend")
    s.add_argument("--project-only", action="store_true")
    sub.add_parser("analyse")
    args = ap.parse_args()

    if args.cmd == "freeze":
        freeze([c.upper() for c in args.countries], args.cap_usd)
        return
    if args.cmd == "data":
        build_data_table([c.upper() for c in args.countries] if args.countries else _frozen_countries())
        return
    if args.cmd == "analyse":
        out = analyse()
        print(json.dumps({k: v.get("branch") for k, v in out.items() if isinstance(v, dict) and "branch" in v},
                         indent=1))
        print(f"spend ${out['spend']['total_usd']}; weekly 48 countries "
              f"${out['spend'].get('weekly_48_countries_usd')}")
        return

    countries = [c.upper() for c in args.countries] if args.countries else _frozen_countries()
    projected = project({arm: args.n for arm in args.arm}, countries)
    print(f"[cost] {args.arm} x{args.n} over {countries}: projected ${projected:.2f} "
          f"against a cap of ${args.cap_usd:.2f}")
    if args.project_only:
        return
    if projected > args.cap_usd:
        raise usage.BudgetExhausted(f"projected ${projected:.2f} is over the ${args.cap_usd:.2f} cap")
    meter = usage.Meter()
    for arm in args.arm:
        score_arm(arm, args.n, countries, meter, args.cap_usd)
    print(meter.summary())


if __name__ == "__main__":
    main()
