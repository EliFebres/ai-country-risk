"""
Measure the scorer on frozen payloads (step 5, and step 5b on the same files).

The payload is fixed first and the scorer is measured second. `freeze` builds
this week's payload for each country through the normal pipeline
(`pipeline.assemble_country`) and writes it to a file with its fingerprint.
Every scoring call after that reads the file. Nothing is re-fetched, so a change
in output comes from the scorer and not from the input.

Step 5b reads the step 5 payloads from `backend/measurements/step5/` and writes
everything new under `backend/measurements/step5b/`, so step 5's record stays
as it was. The data half needs the roster table (`roster`), built once from the
local panel and curated CSV and saved beside the calls.

Nothing here writes `risk_snapshot` or `run_ledger`. Each call's answer goes to
a JSON-lines file under `calls/`, one file per arm and country, and a rerun
appends to it, so an interrupted arm resumes where it stopped.

    python -m backend.llm.scorer_measure roster
    python -m backend.llm.scorer_measure score --arm full --n 10 --cap-usd 3
    python -m backend.llm.scorer_measure score --arm no_articles --arm no_economics --n 3 --cap-usd 1
    python -m backend.llm.scorer_measure score --arm first_5 --arm first_10 --arm first_15 --n 3 --cap-usd 2
    python -m backend.llm.scorer_measure analyse

The arms:

- `full`: the frozen payload as it is.
- `full_confirm`: the same payload, in a separate file, for the median-of-3
  confirmation: every three consecutive calls are one median.
- `no_articles`: the economics block and structural facts, with no articles, as
  the pipeline would send a week in which nothing passed the gate.
- `no_economics`: the articles and structural facts, with every ledger's
  indicators removed and the pipeline's own empty-ledger note in their place.
- `first_5`, `first_10`, `first_15`: the first n selected articles, in
  selection order, with the counts and coverage recomputed for the subset.
  `full` is the "all" point.

It prints counts. Titles and verdicts stay in the files.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import itertools
import json
import statistics as st
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.util import env
env.load()

from backend.llm import combine, data_score
from backend.llm import constants as ai_constants
from backend.llm import langchain_llm, payload_health, relevance
from backend.llm.payload import count_tokens
from backend.util import constants, paths, pipeline, usage

__all__ = ["freeze", "ARMS", "score_arm", "analyse"]

FROZEN_DIR = paths.PROJECT_ROOT / "backend" / "measurements" / "step5"
OUT_DIR = paths.PROJECT_ROOT / "backend" / "measurements" / "step5b"
CALLS_DIR = OUT_DIR / "calls"
ROSTER_PATH = OUT_DIR / "roster_percentiles.json"

SCORING_MODEL = pipeline.SCORING_MODEL
SEED = 42

# The empty-ledger note, word for word as `payload.build_economics_block`
# writes it, so `no_economics` reads like a real week with nothing resolved.
EMPTY_LEDGER_NOTE = (
    "No indicator resolved for this ledger. Nothing is known about it "
    "from the numbers this run; judge it from the articles alone, and "
    "do not read the absence as a good result."
)


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


# --- The roster table ---------------------------------------------------------


def build_roster() -> Dict[str, Any]:
    """Build the roster table and every country's percentile per code; save both."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    table = data_score.roster_table()
    per_country: Dict[str, Dict[str, Any]] = {}
    for iso2 in (c["iso2"] for c in constants.COUNTRY_ROSTER):
        row = {}
        for code in constants.INDICATOR_REGISTRY:
            v = table["values"][code].get(iso2)
            got = data_score.percentile_of(code, v, iso2, table) if v is not None else None
            row[code] = got["percentile"] if got else None
        per_country[iso2] = row
    out = {"table": table, "percentiles_by_country": per_country}
    ROSTER_PATH.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    n = table["n_by_code"]
    print(f"[roster] {len(constants.COUNTRY_ROSTER)} countries; values per code: "
          + ", ".join(f"{c.split('.')[0]}={n[c]}" for c in n)
          + (f"; no panel for {table['missing_countries']}" if table["missing_countries"] else ""))
    return out


def load_roster() -> Dict[str, Any]:
    if not ROSTER_PATH.exists():
        raise RuntimeError(
            f"{ROSTER_PATH} is missing; run `python -m backend.llm.scorer_measure roster` first"
        )
    return json.loads(ROSTER_PATH.read_text(encoding="utf-8"))["table"]


# --- Arms -------------------------------------------------------------------


def _set_components(p: Dict[str, Any], **changes: float) -> None:
    comps = p["evidence_coverage_components"]["components"]
    comps.update({k: round(v, 4) for k, v in changes.items()})


def arm_full(frozen: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(frozen["payload"])


def arm_no_articles(frozen: Dict[str, Any]) -> Dict[str, Any]:
    p = copy.deepcopy(frozen["payload"])
    p["articles"] = []
    p["full_texts"] = []
    cov = p["article_coverage"]
    cov["selected_per_theme"] = {k: 0 for k in cov["selected_per_theme"]}
    cov["selected_per_ledger"] = {k: 0 for k in cov["selected_per_ledger"]}
    cov["passed_the_gate"] = 0
    cov["selected"] = 0
    _set_components(p, article_volume=0.0, body_depth=0.0)
    return p


def arm_no_economics(frozen: Dict[str, Any]) -> Dict[str, Any]:
    p = copy.deepcopy(frozen["payload"])
    for block in p["economics_by_ledger"].values():
        block["indicators"] = []
        block["note"] = EMPTY_LEDGER_NOTE
    _set_components(p, indicator_resolution=0.0, freshness=0.0)
    return p


def _first(n: int) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def arm(frozen: Dict[str, Any]) -> Dict[str, Any]:
        p = copy.deepcopy(frozen["payload"])
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
    "full_confirm": arm_full,
    "no_articles": arm_no_articles,
    "no_economics": arm_no_economics,
    "first_5": _first(5),
    "first_10": _first(10),
    "first_15": _first(15),
}


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


def _data_for(frozen: Dict[str, Any], payload: Dict[str, Any], roster: Dict[str, Any]) -> Dict[str, Any]:
    """The data half for one arm's payload: what the arm left in the block."""
    return data_score.ledger_data_scores(payload["economics_by_ledger"], frozen["iso2"], roster)


def _score(frozen: Dict[str, Any], payload: Dict[str, Any], answer: Dict[str, Any],
           roster: Dict[str, Any], validation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return combine.combine(
        answer=answer, data=_data_for(frozen, payload, roster),
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
            # Sixteen answers with a note each, events and a summary: about 900
            # output tokens, plus a little per article cited.
            tokens_out = 900 + 10 * len(p["articles"])
            total += usage.cost_usd(SCORING_MODEL, todo * tokens_in, todo * tokens_out)
    return total


def score_arm(arm: str, n: int, countries: List[str], meter: usage.Meter, cap_usd: float) -> None:
    CALLS_DIR.mkdir(parents=True, exist_ok=True)
    roster = load_roster()
    for iso2 in countries:
        frozen = load_frozen(iso2)
        payload = ARMS[arm](frozen)
        ids = [a["id"] for a in payload["articles"]]
        sent_sha = _sha(_sent_bytes(payload))
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
                    scoring = _score(frozen, payload, scored["answer"], roster,
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


def _levels(row: Dict[str, Any]) -> Dict[str, Optional[int]]:
    return {q: v.get("level") for q, v in row["scoring"]["questions"].items()}


def _question_agreement(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per question: the modal level, how many calls gave it, and the null share."""
    out = {}
    for qid in ai_constants.QUESTION_IDS:
        levels = [_levels(r).get(qid) for r in rows]
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


def _median_of_triples(iso2: str, rows: List[Dict[str, Any]], roster: Dict[str, Any]) -> Dict[str, Any]:
    """`score_12m` for the median-of-3 of every triple of the M1 calls, at no cost."""
    frozen = load_frozen(iso2)
    payload = arm_full(frozen)
    scores = []
    for triple in itertools.combinations(rows, 3):
        merged = combine.merge_answers([r["answer"] for r in triple])
        scores.append(_score(frozen, payload, merged, roster)["score_12m"])
    return {"triples": len(scores), **_stats(scores)}


def _confirm_medians(iso2: str, roster: Dict[str, Any]) -> Dict[str, Any]:
    """Fresh median-of-3 sets from `full_confirm`: every three consecutive calls."""
    rows = _scored("full_confirm", iso2)
    frozen = load_frozen(iso2)
    payload = arm_full(frozen)
    scores = []
    for k in range(0, len(rows) - len(rows) % 3, 3):
        merged = combine.merge_answers([r["answer"] for r in rows[k:k + 3]])
        scores.append(_score(frozen, payload, merged, roster)["score_12m"])
    return {"sets": len(scores), **_stats(scores)}


def analyse() -> Dict[str, Any]:
    """M1, face validity, median-of-3, M3, M4, M5 and dilution. Writes analysis.json."""
    countries = [c for c in _frozen_countries() if _scored("full", c)]
    roster = load_roster()
    out: Dict[str, Any] = {"countries": countries, "prompt_version": ai_constants.PROMPT_VERSION}

    # M1: repeatability, on the computed score and per question.
    m1: Dict[str, Any] = {}
    for iso2 in countries:
        rows = _scored("full", iso2)
        sc = [r["scoring"] for r in rows]
        counts = Counter()
        for r in rows:
            counts.update(r.get("counts") or {})
        agreement = _question_agreement(rows)
        m1[iso2] = {
            "calls": len(rows),
            "failed": len(read_calls("full", iso2)) - len(rows),
            "score_12m": _stats([s["score_12m"] for s in sc]),
            "score_3m": _stats([s["score_3m"] for s in sc]),
            **{f: _stats([s["ledger_ints"].get(f) for s in sc]) for f in LEDGER_SCORES},
            "news_scores": {f: _stats([s["news_scores"][f]["score"] for s in sc]) for f in LEDGER_SCORES},
            "data_scores": {f: sc[0]["data_scores"].get(f) for f in LEDGER_SCORES} if sc else {},
            "questions": agreement,
            "questions_flipping": [q for q, a in agreement.items() if a["flips"]],
            "validator_counts": dict(counts),
            "events_per_call": [len(s["events"]["valid"]) for s in sc],
        }
    worst = max((m1[c]["score_12m"].get("sd", 0) for c in countries), default=0)
    out["m1"] = {"per_country": m1, "worst_sd_12m": worst,
                 "branch": ("one call" if worst <= 1 else
                            "median of 3" if worst <= 3 else "stop")}

    # Face validity: RU highest, PT lowest or second lowest, on the median.
    medians = {c: m1[c]["score_12m"].get("median") for c in countries if m1[c]["score_12m"].get("n")}
    order = sorted(medians, key=lambda c: medians[c], reverse=True)
    fv = {"order": [(c, medians[c]) for c in order]}
    if medians:
        fv["ru_highest"] = order[0] == "RU"
        fv["pt_rank_from_bottom"] = (len(order) - order.index("PT")) if "PT" in order else None
        fv["pass"] = bool(fv["ru_highest"] and fv["pt_rank_from_bottom"] in (1, 2))
        fv["ledger_medians"] = {
            c: {f: m1[c][f].get("median") for f in LEDGER_SCORES} for c in countries
        }
        fv["branch"] = "pass" if fv["pass"] else "fail"
    out["face_validity"] = fv

    # Median of 3: from triples of the M1 calls (free), then fresh sets.
    mo3: Dict[str, Any] = {"from_triples": {}, "fresh": {}}
    for iso2 in countries:
        rows = _scored("full", iso2)
        if len(rows) >= 3:
            mo3["from_triples"][iso2] = _median_of_triples(iso2, rows, roster)
        if _scored("full_confirm", iso2):
            mo3["fresh"][iso2] = _confirm_medians(iso2, roster)
    for key in ("from_triples", "fresh"):
        sds = [v.get("sd") for v in mo3[key].values() if v.get("sd") is not None]
        mo3[f"worst_sd_{key}"] = max(sds) if sds else None
    out["median_of_3"] = mo3
    out["m2"] = {"note": "the composite is computed; the grid is not measured"}

    # M3: do the articles move the score? With no articles the news half must
    # be null and the score is the data half alone.
    m3: Dict[str, Any] = {}
    for iso2 in countries:
        full = st.median([r["scoring"]["score_12m"] for r in _scored("full", iso2)])
        row: Dict[str, Any] = {"full_median_12m": full}
        for arm in ("no_articles", "no_economics"):
            rows = _scored(arm, iso2)
            if not rows:
                continue
            vals = [r["scoring"]["score_12m"] for r in rows]
            entry: Dict[str, Any] = {"values": vals, "median": st.median(vals),
                                     "shift": st.median(vals) - full}
            if arm == "no_articles":
                raw_non_null = [sum(1 for q in ai_constants.QUESTION_IDS
                                    if ((r.get("raw") or {}).get(q) or {}).get("level") is not None)
                                for r in rows]
                entry["raw_non_null_levels"] = raw_non_null
                entry["validator_failures"] = sum(raw_non_null)
                entry["final_non_null_levels"] = [
                    sum(v is not None for v in _levels(r).values()) for r in rows]
                entry["ledger_modes"] = [
                    {f: r["scoring"]["ledgers"][f]["mode"] for f in LEDGER_SCORES} for r in rows]
                entry["equals_data_half"] = all(
                    all(m in ("data_only", "none") for m in modes.values()) for modes in entry["ledger_modes"])
            else:
                entry["ledger_modes"] = [
                    {f: r["scoring"]["ledgers"][f]["mode"] for f in LEDGER_SCORES} for r in rows]
            row[arm] = entry
        m3[iso2] = row

    def _mean(arm, fn):
        xs = [fn(m3[c][arm]) for c in countries if arm in m3[c]]
        return round(sum(xs) / len(xs), 2) if xs else None
    out["m3"] = {
        "per_country": m3,
        "no_articles_mean_abs_shift": _mean("no_articles", lambda e: abs(e["shift"])),
        "no_articles_mean_signed_shift": _mean("no_articles", lambda e: e["shift"]),
        "no_articles_validator_failures": sum(
            m3[c]["no_articles"]["validator_failures"] for c in countries if "no_articles" in m3[c]),
        "no_economics_mean_abs_shift": _mean("no_economics", lambda e: abs(e["shift"])),
        "no_economics_mean_signed_shift": _mean("no_economics", lambda e: e["shift"]),
    }
    if out["m3"]["no_articles_mean_abs_shift"] is not None:
        out["m3"]["branch"] = (f"articles move the score by {out['m3']['no_articles_mean_abs_shift']} on average; "
                               f"{out['m3']['no_articles_validator_failures']} levels cited with no articles")

    # M4: the 3-month score. Every non-zero gap traces to a dated event.
    gaps: List[Dict[str, Any]] = []
    for iso2 in countries:
        for arm in ("full", "no_articles", "no_economics"):
            for r in _scored(arm, iso2):
                s = r["scoring"]
                gaps.append({
                    "iso2": iso2, "arm": arm, "i": r["i"],
                    "gap": s["score_12m"] - s["score_3m"],
                    "events": [{k: e.get(k) for k in ("article_id", "date", "question", "direction", "event")}
                               for e in s["events"]["valid"]],
                    "events_dropped": (r.get("counts") or {}).get("events_dropped", 0),
                })
    dist = Counter(g["gap"] for g in gaps)
    modal = dist.most_common(1)[0][0] if dist else None
    out["m4"] = {
        "calls": len(gaps),
        "distribution": dict(sorted(dist.items())),
        "modal_gap": modal,
        "modal_share": round(dist[modal] / len(gaps), 3) if gaps else None,
        "non_zero": [g for g in gaps if g["gap"]],
        "all_traced": all(g["events"] for g in gaps if g["gap"]),
        "events_dropped_total": sum(g["events_dropped"] for g in gaps),
    }
    if gaps:
        out["m4"]["branch"] = (f"{sum(1 for g in gaps if g['gap'])} of {len(gaps)} calls have a gap; "
                               + ("every one traces to a dated event" if out["m4"]["all_traced"]
                                  else "A GAP WITH NO EVENT"))

    # M5: article count. "All" is the first three M1 calls, the same payload.
    per_country: Dict[str, Any] = {}
    pooled_fe: List[tuple] = []
    pooled_raw: List[tuple] = []
    by_count_levels: Dict[int, List[Dict[str, Optional[int]]]] = {}
    for iso2 in countries:
        pts: List[tuple] = []
        n_all = len(load_frozen(iso2)["article_ids"])
        for arm, n in (("first_5", 5), ("first_10", 10), ("first_15", 15), ("full", n_all)):
            limit = 3 if arm == "full" else None
            for r in _scored(arm, iso2, limit):
                x = min(n, n_all)
                pts.append((x, r["scoring"]["score_12m"]))
                by_count_levels.setdefault(x, []).append(_levels(r))
        if len({x for x, _ in pts}) < 2:
            continue
        medians = {x: st.median([y for xx, y in pts if xx == x]) for x in sorted({x for x, _ in pts})}
        my = sum(y for _, y in pts) / len(pts)
        mx = sum(x for x, _ in pts) / len(pts)
        pooled_fe += [(x - mx, y - my) for x, y in pts]
        pooled_raw += pts
        per_country[iso2] = {"medians_by_count": medians, "slope": round(_slope(pts), 3) if _slope(pts) is not None else None}
    pooled = _slope(pooled_fe)
    per_question: Dict[str, Any] = {}
    for qid in ai_constants.QUESTION_IDS:
        per_question[qid] = {
            str(x): {
                "non_null_share": round(sum(l.get(qid) is not None for l in ls) / len(ls), 2),
                "mean_level": (round(st.mean([l[qid] for l in ls if l.get(qid) is not None]), 2)
                               if any(l.get(qid) is not None for l in ls) else None),
            }
            for x, ls in sorted(by_count_levels.items())
        }
    out["m5"] = {
        "per_country": per_country,
        "pooled_slope_within_country": round(pooled, 3) if pooled is not None else None,
        "pooled_slope_naive": (round(_slope(pooled_raw), 3) if _slope(pooled_raw) is not None else None),
        "per_question_by_count": per_question,
    }
    if pooled is not None:
        out["m5"]["branch"] = ("volume question closed" if abs(pooled) <= 0.2
                               else "volume moves the score")

    # Dilution: article weight against the gate's test code, and uncited share.
    by_test: Dict[str, List[int]] = {}
    uncited: List[float] = []
    for iso2 in countries:
        test_of = {m["id"]: m.get("test") for m in load_frozen(iso2)["articles_meta"]}
        for arm in ("full", "no_economics", "first_5", "first_10", "first_15"):
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
    t1 = out["dilution"]["by_test"].get("T1")
    if t1:
        out["dilution"]["branch"] = ("rank T1 below T2-T5" if t1["share_zero"] > 0.5
                                     else "T1 informs the score")

    # Spend, metered from the call rows.
    spend: Dict[str, Any] = {}
    for arm in ARMS:
        rows = [r for c in countries for r in read_calls(arm, c)]
        if rows:
            tin = sum(r.get("input_tokens") or 0 for r in rows)
            tout = sum(r.get("output_tokens") or 0 for r in rows)
            spend[arm] = {"calls": len(rows), "input_tokens": tin, "output_tokens": tout,
                          "usd": round(usage.cost_usd(SCORING_MODEL, tin, tout), 3)}
    spend["total_usd"] = round(sum(v["usd"] for v in spend.values()), 3)
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
    sub.add_parser("roster")
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
    if args.cmd == "roster":
        build_roster()
        return
    if args.cmd == "analyse":
        out = analyse()
        print(json.dumps({k: v.get("branch") for k, v in out.items() if isinstance(v, dict) and "branch" in v},
                         indent=1))
        print(f"spend ${out['spend']['total_usd']}")
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
