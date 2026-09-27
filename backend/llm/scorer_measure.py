"""
Measure the scorer on frozen payloads (step 5).

The payload is fixed first and the scorer is measured second. `freeze` builds
this week's payload for each country through the normal pipeline
(`pipeline.assemble_country`) and writes it to a file with its fingerprint.
Every scoring call after that reads the file. Nothing is re-fetched, so a change
in output comes from the scorer and not from the input.

Nothing here writes `risk_snapshot` or `run_ledger`. Each call's answer goes to
a JSON-lines file under `backend/measurements/step5/calls/`, one file per arm
and country, and a rerun appends to it, so an interrupted arm resumes where it
stopped.

    python -m backend.llm.scorer_measure freeze US PT KW HK TW RU
    python -m backend.llm.scorer_measure score --arm full --n 10
    python -m backend.llm.scorer_measure score --arm no_articles --n 3
    python -m backend.llm.scorer_measure analyse

The arms:

- `full`: the frozen payload as it is.
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
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.util import env
env.load()

from backend.llm import constants as ai_constants
from backend.llm import langchain_llm, payload_health, relevance
from backend.llm.payload import count_tokens
from backend.util import constants, paths, pipeline, usage

__all__ = ["freeze", "ARMS", "score_arm"]

OUT_DIR = paths.PROJECT_ROOT / "backend" / "measurements" / "step5"
CALLS_DIR = OUT_DIR / "calls"

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
    return OUT_DIR / f"payload_{iso2}.json"


def _sent_bytes(payload: Dict[str, Any]) -> str:
    """The user message exactly as `score_country` serializes it."""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Part 2: freeze ---------------------------------------------------------


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
        _payload_path(iso2).write_text(
            json.dumps(frozen, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
        )
        # Reading it back must give the same bytes to the scorer.
        again = json.loads(_payload_path(iso2).read_text(encoding="utf-8"))
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


def project(arms: Dict[str, int], countries: List[str]) -> float:
    """Projected cost of the remaining calls, from the payloads' real sizes."""
    prompt_tokens = count_tokens(ai_constants.RISK_PROMPT)["tokens"]
    total = 0.0
    for iso2 in countries:
        frozen = load_frozen(iso2)
        for arm, n in arms.items():
            todo = max(n - len(read_calls(arm, iso2)), 0)
            if not todo:
                continue
            p = ARMS[arm](frozen)
            tokens_in = prompt_tokens + count_tokens(_sent_bytes(p))["tokens"] + 50
            # Measured on the first calls: about 70 output tokens per article
            # plus about 450 for the rest of the answer.
            tokens_out = 450 + 70 * len(p["articles"])
            total += usage.cost_usd(SCORING_MODEL, todo * tokens_in, todo * tokens_out)
    return total


def score_arm(arm: str, n: int, countries: List[str], meter: usage.Meter, cap_usd: float) -> None:
    CALLS_DIR.mkdir(parents=True, exist_ok=True)
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
            )
            row = {
                "arm": arm, "iso2": iso2, "i": i,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "payload_sha256": sent_sha,
                "prompt_version": ai_constants.PROMPT_VERSION,
                "failed": scored["failed"],
                "answer": scored["answer"],
                "raw": scored["raw"],
                "violations": scored["violations"],
                "input_tokens": meter.input_tokens - before[0],
                "output_tokens": meter.output_tokens - before[1],
            }
            with _calls_path(arm, iso2).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            a = scored["answer"] or {}
            print(f"[score] {arm} {iso2} #{i + 1}: 12m={a.get('score_12m')} 3m={a.get('score_3m')} "
                  f"({'failed: ' + scored['failed'][:60] if scored['failed'] else 'ok'}) "
                  f"${meter.spend_usd:.3f} so far")
            meter.check(cap_usd)


# --- Analysis ---------------------------------------------------------------

FIELDS = ("score_12m", "score_3m", "friction", "order", "information", "edge")
COMPOSITES = ("score_12m", "score_3m")
LEDGER_SCORES = ("friction", "order", "information", "edge")

# The prompt's anchor bands meet at these pairs (8-22 | 23-38 | 39-54 | 55-69 |
# 70-84 | 85-98). A score is "near an edge" within 2 points of either side.
BAND_EDGES = (22, 23, 38, 39, 54, 55, 69, 70, 84, 85)
EDGE_WINDOW = 2


def _near_edge(v: int) -> bool:
    return min(abs(v - e) for e in BAND_EDGES) <= EDGE_WINDOW


def _stats(values: List[float]) -> Dict[str, Any]:
    import statistics as st
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "distinct": sorted(set(vals)),
        "range": max(vals) - min(vals),
        "sd": round(st.stdev(vals), 3) if len(vals) > 1 else 0.0,
        "median": st.median(vals),
        "mean": round(st.mean(vals), 2),
    }


def _answers(arm: str, iso2: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = [r for r in read_calls(arm, iso2) if r.get("answer")]
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


def _snapshot_rows() -> List[Dict[str, Any]]:
    """Every `risk_snapshot` row in dev, read-only."""
    from backend.util import db
    if db.resolve() != "dev":
        raise RuntimeError("the analysis reads dev only")
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT country_iso2, run_date, score_12m, score_3m, friction_score,
                              order_score, information_score, edge_score
                       FROM risk_snapshot""")
        rows = [dict(zip(("iso2", "run_date", *FIELDS), r)) for r in cur.fetchall()]
        conn.rollback()
    return rows


def analyse(scheduled: Dict[str, bool]) -> Dict[str, Any]:
    """M1-M5 and the dilution check, from the call files. Writes analysis.json."""
    import statistics as st
    countries = sorted(p.stem.split("_", 1)[1] for p in OUT_DIR.glob("payload_*.json"))
    out: Dict[str, Any] = {"countries": countries}

    # M1: repeatability.
    m1: Dict[str, Any] = {}
    for iso2 in countries:
        rows = _answers("full", iso2)
        answers = [r["answer"] for r in rows]
        flags = [json.dumps(a.get("condition_flags"), sort_keys=True) for a in answers]
        raws = [json.dumps(r["raw"], sort_keys=True) for r in rows]
        modal_flags = max(set(flags), key=flags.count) if flags else None
        modal_raw = max(set(raws), key=raws.count) if raws else None
        m1[iso2] = {
            "calls": len(rows),
            "failed": len(read_calls("full", iso2)) - len(rows),
            **{f: _stats([a.get(f) for a in answers]) for f in FIELDS},
            "identical_flags": flags.count(modal_flags) if flags else 0,
            "exact_json_matches_mode": raws.count(modal_raw) if raws else 0,
            "distinct_json": len(set(raws)),
        }
    worst = max((m1[c]["score_12m"].get("sd", 0) for c in countries), default=0)
    out["m1"] = {"per_country": m1, "worst_sd_12m": worst,
                 "branch": ("one call" if worst <= 1 else
                            "median of 3" if worst <= 3 else "stop")}

    # M2: the grid.
    m1_answers = [r["answer"] for c in countries for r in _answers("full", c)]
    snaps = _snapshot_rows()
    pool = m1_answers + snaps
    def share5(fields):
        vals = [a.get(f) for a in pool for f in fields if a.get(f) is not None]
        return {"n": len(vals), "multiples_of_5": sum(v % 5 == 0 for v in vals),
                "share": round(sum(v % 5 == 0 for v in vals) / len(vals), 3) if vals else None}
    s12 = [a["score_12m"] for a in pool if a.get("score_12m") is not None]
    near = sum(_near_edge(v) for v in s12)
    base = sum(_near_edge(v) for v in range(8, 99)) / len(range(8, 99))
    out["m2"] = {
        "sources": {"m1_calls": len(m1_answers), "risk_snapshot_rows": len(snaps)},
        "distinct": {f: sorted({a.get(f) for a in pool if a.get(f) is not None}) for f in FIELDS},
        "composites": share5(COMPOSITES),
        "ledgers": share5(LEDGER_SCORES),
        "per_ledger": {f: share5((f,)) for f in LEDGER_SCORES},
        "score_12m_near_edge": near, "score_12m_n": len(s12),
        "near_edge_share": round(near / len(s12), 3) if s12 else None,
        "near_edge_base_rate_uniform_8_98": round(base, 3),
        "branch": "anchors act as buckets" if s12 and near / len(s12) > 0.5 else "no bucket finding",
    }

    # M3: do the articles move the score?
    m3: Dict[str, Any] = {}
    for iso2 in countries:
        full = st.median([a["score_12m"] for a in (r["answer"] for r in _answers("full", iso2))])
        row = {"full_median_12m": full}
        for arm in ("no_articles", "no_economics"):
            vals = [r["answer"]["score_12m"] for r in _answers(arm, iso2)]
            if vals:
                row[arm] = {"values": vals, "median": st.median(vals),
                            "shift": st.median(vals) - full}
        m3[iso2] = row
    def mean_abs(arm):
        shifts = [abs(m3[c][arm]["shift"]) for c in countries if arm in m3[c]]
        return round(sum(shifts) / len(shifts), 2) if shifts else None
    def mean_signed(arm):
        shifts = [m3[c][arm]["shift"] for c in countries if arm in m3[c]]
        return round(sum(shifts) / len(shifts), 2) if shifts else None
    out["m3"] = {
        "per_country": m3,
        "no_articles_mean_abs_shift": mean_abs("no_articles"),
        "no_articles_mean_signed_shift": mean_signed("no_articles"),
        "no_economics_mean_abs_shift": mean_abs("no_economics"),
        "no_economics_mean_signed_shift": mean_signed("no_economics"),
    }
    if out["m3"]["no_articles_mean_abs_shift"] is not None:
        out["m3"]["branch"] = ("articles not driving the score"
                               if out["m3"]["no_articles_mean_abs_shift"] < 3
                               else "articles move the score")

    # M4: the 3-month score.
    gaps: List[Dict[str, Any]] = []
    for iso2 in countries:
        for arm in ("full", "no_articles", "no_economics"):
            for r in _answers(arm, iso2):
                a = r["answer"]
                gaps.append({"iso2": iso2, "arm": arm, "gap": a["score_12m"] - a["score_3m"]})
    dist: Dict[int, int] = {}
    for g in gaps:
        dist[g["gap"]] = dist.get(g["gap"], 0) + 1
    modal = max(dist, key=dist.get) if dist else None
    by_sched: Dict[str, Any] = {}
    for flag in (True, False):
        gs = [g["gap"] for g in gaps if scheduled.get(g["iso2"]) is flag]
        by_sched["scheduled" if flag else "none"] = {
            "countries": [c for c in countries if scheduled.get(c) is flag],
            **({"mean_gap": round(sum(gs) / len(gs), 2),
                "distribution": {k: gs.count(k) for k in sorted(set(gs))}} if gs else {}),
        }
    out["m4"] = {
        "calls": len(gaps),
        "distribution": dict(sorted(dist.items())),
        "modal_gap": modal,
        "modal_share": round(dist[modal] / len(gaps), 3) if gaps else None,
        "by_scheduled_event": by_sched,
    }
    if gaps:
        out["m4"]["branch"] = ("score_3m carries no information"
                               if dist[modal] / len(gaps) > 0.8 else "gap varies")

    # M5: article count. "All" is the first three M1 calls, the same payload.
    per_country: Dict[str, Any] = {}
    pooled_fe: List[tuple] = []
    pooled_raw: List[tuple] = []
    for iso2 in countries:
        pts: List[tuple] = []
        n_all = len(load_frozen(iso2)["article_ids"])
        for arm, n in (("first_5", 5), ("first_10", 10), ("first_15", 15), ("full", n_all)):
            limit = 3 if arm == "full" else None
            for r in _answers(arm, iso2, limit):
                pts.append((min(n, n_all), r["answer"]["score_12m"]))
        if not pts:
            continue
        medians = {}
        for x in sorted({x for x, _ in pts}):
            medians[x] = st.median([y for xx, y in pts if xx == x])
        my = sum(y for _, y in pts) / len(pts)
        mx = sum(x for x, _ in pts) / len(pts)
        pooled_fe += [(x - mx, y - my) for x, y in pts]
        pooled_raw += pts
        per_country[iso2] = {"medians_by_count": medians, "slope": _slope(pts)}
    if len({x for x, _ in pooled_raw}) < 2:
        per_country, pooled_fe, pooled_raw = {}, [], []
    pooled = _slope(pooled_fe)
    out["m5"] = {
        "per_country": per_country,
        "pooled_slope_within_country": round(pooled, 3) if pooled is not None else None,
        "pooled_slope_naive": (round(_slope(pooled_raw), 3)
                               if _slope(pooled_raw) is not None else None),
    }
    if pooled is not None:
        out["m5"]["branch"] = "no action" if abs(pooled) <= 0.2 else "volume moves the score"

    # Dilution: scorer bearing against the gate's test code.
    by_test: Dict[str, List[int]] = {}
    for iso2 in countries:
        test_of = {m["id"]: m.get("test") for m in load_frozen(iso2)["articles_meta"]}
        for arm in ("full", "no_economics", "first_5", "first_10", "first_15"):
            for r in _answers(arm, iso2):
                for s in r["answer"].get("article_scores") or []:
                    t = test_of.get(s.get("id")) or "?"
                    if s.get("bearing") is not None:
                        by_test.setdefault(t, []).append(s["bearing"])
    out["dilution"] = {
        t: {"appearances": len(v), "mean_bearing": round(sum(v) / len(v), 2),
            "share_zero": round(sum(b == 0 for b in v) / len(v), 3)}
        for t, v in sorted(by_test.items())
    }
    t1 = out["dilution"].get("T1")
    if t1:
        out["dilution_branch"] = ("rank T1 below T2-T5" if t1["share_zero"] > 0.5
                                  else "T1 informs the score")

    (OUT_DIR / "analysis.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("countries", nargs="+")
    f.add_argument("--cap-usd", type=float, default=1.5)
    s = sub.add_parser("score")
    s.add_argument("--arm", action="append", required=True, choices=sorted(ARMS))
    s.add_argument("--n", type=int, required=True)
    s.add_argument("--countries", nargs="+", default=None)
    s.add_argument("--cap-usd", type=float, required=True,
                   help="cap on this invocation's metered spend")
    s.add_argument("--project-only", action="store_true")
    a = sub.add_parser("analyse")
    a.add_argument("--scheduled", default="",
                   help="ISO-2 codes whose payload holds a dated, scheduled event "
                        "inside three months, read from the frozen payloads")
    args = ap.parse_args()

    if args.cmd == "freeze":
        freeze([c.upper() for c in args.countries], args.cap_usd)
        return
    if args.cmd == "analyse":
        marked = {c.strip().upper() for c in args.scheduled.split(",") if c.strip()}
        countries = sorted(p.stem.split("_", 1)[1] for p in OUT_DIR.glob("payload_*.json"))
        out = analyse({c: c in marked for c in countries})
        print(json.dumps({k: v.get("branch") if isinstance(v, dict) else v
                          for k, v in out.items() if k != "dilution"}, indent=1))
        return

    countries = args.countries or sorted(
        p.stem.split("_", 1)[1] for p in OUT_DIR.glob("payload_*.json")
    )
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
