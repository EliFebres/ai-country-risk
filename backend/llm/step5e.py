"""
Step 5e: eight scorer fixes measured, and the benchmark set re-frozen.

One script for the session's measurement, written up in
docs/scorer-checklist.md (Step 5e). It never writes `risk_snapshot` or
`run_ledger`; `assemble_country` writes the article, series and cache
tables as the weekly run does.

    python -m backend.llm.step5e select-check [--countries ...] [--cap-usd 2]
        Retrieval, gate and story check once per country; then the old
        selection rules (kept only here) and the new ones on the same
        candidates. Saves the six new payloads twice (masked, named) in the
        step 5 frozen format, and `selection_check.json`.
    python -m backend.llm.step5e score --arm {old_named,new_masked,new_named} --merges N
                                       [--countries ...] [--cap-usd 18] [--stop-usd X]
        N production-mode merges per country (`scoring.run_scoring`, 3 to 7
        calls each). Rows to calls/<arm>_<iso2>.jsonl, merges to
        merges/<arm>_<iso2>.jsonl. Resumes from the merges already made.
    python -m backend.llm.step5e revalidate --arm ARM --quote-match ordered_90
        Re-run the validator on the stored raw answers under another quote
        rule and re-merge, into arm `<ARM>.<rule>`. No model call.
    python -m backend.llm.step5e probe
        One gpt-4o-mini call per masked payload: which country is this?
    python -m backend.llm.step5e analyse
        Every pre-set decision, one line each, and analysis.json.
    python -m backend.llm.step5e reference
        The first masked merge per country as reference_<iso2>.json.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import statistics as st
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from backend.util import env
env.load()

from backend.llm import combine, constants as ai_constants, data_score, langchain_llm, relevance
from backend.llm import scoring as scoring_rule
from backend.llm import validate
from backend.llm import scorer_measure as sm
from backend.llm.payload import count_tokens
from backend.util import constants, gazetteer, paths, pipeline, usage
from backend.news_fetching import core

OUT_DIR = paths.PROJECT_ROOT / "backend" / "measurements" / "step5e"
CALLS_DIR = OUT_DIR / "calls"
MERGES_DIR = OUT_DIR / "merges"
OLD_DIR = paths.PROJECT_ROOT / "backend" / "measurements" / "step5"
OLD_CALLS_DIR = paths.PROJECT_ROOT / "backend" / "measurements" / "step5d" / "calls"
OLD_DATA_PATH = paths.PROJECT_ROOT / "backend" / "measurements" / "step5d" / "data_table.json"
OLD_ANALYSIS_PATH = paths.PROJECT_ROOT / "backend" / "measurements" / "step5d" / "analysis.json"

COUNTRIES = ["US", "PT", "KW", "HK", "TW", "RU"]
SCORING_MODEL = pipeline.SCORING_MODEL
PROBE_MODEL = "gpt-4o-mini-2024-07-18"
SEED = 42
ARMS = ("old_named", "new_masked", "new_named")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


# --- Part 2: selection check and the six payloads ------------------------------


def _select_old(articles, labels, iso2, *, budget, same_story_as):
    """Selection as it stood before step 5e, kept here only for the
    comparison: no spillover sort or cap, cut at the budget, and no check
    after selection. A copy of the pre-5e `relevance.select` ordering."""
    eligible, dropped = [], set(same_story_as or {})
    for a in articles:
        v = labels.get(relevance.relevance_key(a, iso2))
        if v and v.get("label") == "relevant":
            e = dict(a)
            e["relevance"] = v
            eligible.append(e)
    pool = [a for a in eligible if (a.get("publisher_link") or a.get("link")) not in dropped]

    def order_key(a):
        return (relevance._published_sort_key(a), a.get("publisher_link") or a.get("link") or "")

    events = sorted([a for a in pool if a["relevance"].get("high_impact_event")], key=order_key, reverse=True)
    rest = sorted([a for a in pool if not a["relevance"].get("high_impact_event")], key=order_key, reverse=True)
    buckets = {led: [] for led in relevance.LEDGERS}
    buckets["(none)"] = []
    for a in rest:
        led = a["relevance"].get("ledgers") or []
        if not led:
            buckets["(none)"].append(a)
        for name in led:
            if name in buckets:
                buckets[name].append(a)
    selected, taken, pubs = [], set(), {}

    def take(a):
        url = a.get("publisher_link") or a.get("link") or ""
        if url in taken:
            return False
        taken.add(url)
        pub = (a.get("source") or "").strip()
        pubs[pub] = pubs.get(pub, 0) + 1
        selected.append(a)
        return True

    for a in events:
        if len(selected) >= budget:
            break
        take(a)

    def next_from(bucket):
        fallback = None
        for c in bucket:
            if (c.get("publisher_link") or c.get("link") or "") in taken:
                continue
            if pubs.get((c.get("source") or "").strip(), 0) == 0:
                return c
            if fallback is None:
                fallback = c
        return fallback

    while len(selected) < budget:
        progressed = False
        for name in relevance.LEDGERS:
            if len(selected) >= budget:
                break
            c = next_from(buckets[name])
            if c is not None and take(c):
                progressed = True
        if not progressed:
            break
    for a in buckets["(none)"]:
        if len(selected) >= budget:
            break
        take(a)
    return selected


def _frozen_record(iso2, name, run_as_of, built, payload, mode, extra):
    gate, stories = built["gate"], built["stories"]
    by_url = {r["url"]: r for r in gate["gate_labels"]}
    meta = []
    for it in built["items"]:
        url = it.get("publisher_link") or it.get("link")
        v = by_url.get(url) or {}
        meta.append({
            "id": it.get("id"), "url": url, "title": it.get("title"),
            "masked_title": it.get("masked_title"), "publisher": it.get("source"),
            "published": (it.get("page_published_at") or it.get("published") or "")[:10],
            "test": v.get("test"), "exclusion": v.get("exclusion"), "ledger": v.get("ledger"),
            "high_impact_event": v.get("high_impact_event"), "what_happened": v.get("what_happened"),
            "body_quality": it.get("body_quality"), "body_status": it.get("body_status"),
        })
    sent = sm._sent_bytes(payload)
    return {
        "iso2": iso2, "country": name, "mode": mode, "run_date": run_as_of.isoformat(),
        "payload_fingerprint": built["census"]["payload_fingerprint"],
        "payload_sha256": _sha(sent),
        "payload_tokens": count_tokens(sent)["tokens"],
        "versions": built["census"]["versions"],
        "article_ids": [a["id"] for a in payload["articles"]],
        "articles_meta": meta,
        "economics_resolution": built["econ"]["resolution"],
        "dedup": {"method": stories["method"], "problem": stories["problem"], **stories["counts"],
                  "same_story_as": stories["same_story_as"],
                  "post_selection": built["post_selection_same_story"]},
        "selection": built["selection"],
        "mask": {k: v for k, v in built["mask"].items() if k != "terms"},
        "mask_terms": built["mask"].get("terms", []),
        "data": data_score.ledger_data_scores(built["econ"]["ledgers"]),
        **extra,
        "payload": payload,
    }


def select_check(countries: List[str], cap_usd: float) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meter = usage.Meter()
    run_as_of = dt.datetime.now(dt.timezone.utc).date()
    report: Dict[str, Any] = {"run_date": run_as_of.isoformat(), "countries": {}}
    for iso2 in countries:
        name = constants.COUNTRY_NAME_BY_ISO2[iso2]
        print(f"\n===== {iso2} {name} =====")
        gathered = pipeline.gather_candidates(name, iso2, run_as_of, meter)
        meter.check(cap_usd)

        # New rules, through the production path (writes the caches it would).
        built = pipeline.assemble_country(name, iso2, run_as_of, meter, gathered=gathered)
        meter.check(cap_usd)
        new_items = built["items"]
        new_sel = built["selection"]

        # Old rules, on the same candidates. Digests are needed only to know
        # which of its twenty were headline-only; most are cache hits now.
        from backend.llm import digest_engine
        old_items = _select_old(gathered["candidates"], gathered["labels"], iso2,
                                budget=relevance.ARTICLE_BUDGET,
                                same_story_as=gathered["stories"]["same_story_as"])
        d = digest_engine.digest_articles([a for a in old_items if (a.get("text") or "").strip()], meter=meter)
        old_title_only = 0
        for a in old_items:
            dg = d["digests"].get(a.get("publisher_link") or a.get("link"))
            q = (dg or {}).get("body_quality") if (a.get("text") or "").strip() else None
            a["body_quality"] = q
            if not (a.get("text") or "").strip() or q == "not_article":
                old_title_only += 1
        old_dup = relevance.dedupe_selected(old_items, name, meter=meter)
        new_dup_final = relevance.dedupe_selected(new_items, name, meter=meter)
        meter.check(cap_usd)

        def t5(items):
            return sum(1 for a in items if (a.get("relevance") or {}).get("test") == "T5")

        row = {
            "old": {"multi_article_stories": old_dup["counts"]["multi"],
                    "same_story_dropped_if_checked": old_dup["counts"]["dropped"],
                    "title_only_sent": old_title_only, "t5_sent": t5(old_items),
                    "articles_sent": len(old_items), "story_method": old_dup["method"]},
            "new": {"multi_article_stories": new_dup_final["counts"]["multi"],
                    "title_only_sent": sum(1 for a in new_items if a.get("body_status") == "title-only"),
                    "t5_sent": t5(new_items), "articles_sent": len(new_items),
                    "shortfall": new_sel["shortfall"], "refill_digests": new_sel["refill_digests"],
                    "passes": new_sel["passes"], "story_checks": new_sel["story_checks"],
                    "post_selection_same_story": new_sel["post_selection_same_story"],
                    "title_only_dropped": new_sel["title_only_dropped"],
                    "blocked_after_selection": new_sel["blocked_after_selection"],
                    "final_story_check_clean": new_sel["final_story_check_clean"],
                    "confirmation_check_method": new_dup_final["method"]},
            "mask": {k: v for k, v in built["mask"].items() if k != "terms"},
            "eligible": gathered["stories"]["counts"]["eligible"],
            "candidates": len(gathered["candidates"]),
        }
        report["countries"][iso2] = row
        print(f"[check] {iso2}: old -> new | multi-article stories {row['old']['multi_article_stories']} -> "
              f"{row['new']['multi_article_stories']} | title-only {row['old']['title_only_sent']} -> "
              f"{row['new']['title_only_sent']} | T5 {row['old']['t5_sent']} -> {row['new']['t5_sent']} | "
              f"sent {row['old']['articles_sent']} -> {row['new']['articles_sent']} "
              f"(shortfall {row['new']['shortfall']}) | refill digests {row['new']['refill_digests']}")
        print(f"[check] {iso2}: mask hits {row['mask']['hits']}, regenerated {row['mask']['regenerated']}, "
              f"replaced by code {row['mask']['replaced_by_code']}, publisher mentions "
              f"{row['mask']['publisher_mentions']}, payload leaks {row['mask'].get('payload_leaks')}")

        extra = {"check": row}
        masked = _frozen_record(iso2, name, run_as_of, built, built["scoring_payload"], "masked", extra)
        named = _frozen_record(iso2, name, run_as_of, built, built["scoring_payload_named"], "named", extra)
        _write_json(OUT_DIR / f"payload_{iso2}.json", masked)
        _write_json(OUT_DIR / f"payload_{iso2}_named.json", named)
        for p, rec in ((OUT_DIR / f"payload_{iso2}.json", masked), (OUT_DIR / f"payload_{iso2}_named.json", named)):
            again = json.loads(p.read_text(encoding="utf-8"))
            assert _sha(sm._sent_bytes(again["payload"])) == rec["payload_sha256"], p
        assert not gazetteer.payload_leaks(iso2, masked["payload"]), iso2
        print(f"[freeze] {iso2}: {len(masked['article_ids'])} articles, {masked['payload_tokens']} masked tokens, "
              f"{named['payload_tokens']} named tokens, ${meter.spend_usd:.3f} so far")

    report["spend_usd"] = round(meter.spend_usd, 4)
    report["meter"] = meter.summary()
    print(meter.summary())
    _write_json(OUT_DIR / "selection_check.json", report)
    return report


# --- Part 3: scoring -----------------------------------------------------------


def _load_old(iso2: str) -> Dict[str, Any]:
    frozen = json.loads((OLD_DIR / f"payload_{iso2}.json").read_text(encoding="utf-8"))
    if _sha(sm._sent_bytes(frozen["payload"])) != frozen["payload_sha256"]:
        raise RuntimeError(f"{iso2}: step 5 frozen payload no longer matches its fingerprint")
    return frozen


def _load_new(iso2: str, mode: str) -> Dict[str, Any]:
    path = OUT_DIR / (f"payload_{iso2}.json" if mode == "masked" else f"payload_{iso2}_named.json")
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if _sha(sm._sent_bytes(frozen["payload"])) != frozen["payload_sha256"]:
        raise RuntimeError(f"{path.name}: frozen payload no longer matches its fingerprint")
    return frozen


def _old_as_sent(frozen: Dict[str, Any]) -> Dict[str, Any]:
    """The step 5 payload under the new instrument: no economics block, and
    `as_of` and `window` after `country` with the payload's own run date."""
    p = copy.deepcopy(frozen["payload"])
    p.pop("economics_by_ledger", None)
    out: Dict[str, Any] = {}
    for k, v in p.items():
        out[k] = v
        if k == "country":
            out["as_of"] = frozen["run_date"]
            out["window"] = f"articles published in the {core.WINDOW_DAYS} days before as_of"
    return out


def _arm(arm: str, iso2: str):
    """``(frozen, payload_as_sent, data, masked)`` for an arm."""
    if arm == "old_named":
        frozen = _load_old(iso2)
        data = json.loads(OLD_DATA_PATH.read_text(encoding="utf-8"))["countries"][iso2]["data"]
        return frozen, _old_as_sent(frozen), data, False
    if arm == "new_masked":
        frozen = _load_new(iso2, "masked")
        return frozen, copy.deepcopy(frozen["payload"]), frozen["data"], True
    if arm == "new_named":
        frozen = _load_new(iso2, "named")
        return frozen, copy.deepcopy(frozen["payload"]), frozen["data"], False
    raise ValueError(arm)


def _score(answer, data, article_ids, run_date, validation=None):
    return combine.combine(answer=answer, data=data, article_ids=article_ids,
                           run_date=run_date, history=None, validation=validation)


def read_merges(arm: str, iso2: str) -> List[Dict[str, Any]]:
    return _read_jsonl(MERGES_DIR / f"{arm}_{iso2}.jsonl")


def read_calls(arm: str, iso2: str) -> List[Dict[str, Any]]:
    return _read_jsonl(CALLS_DIR / f"{arm}_{iso2}.jsonl")


def project(arm: str, merges: int, countries: List[str], *, calls_per_merge: float,
            in_tokens: Optional[int] = None, out_tokens: int = 950) -> float:
    total = 0.0
    for iso2 in countries:
        frozen, payload, _, masked = _arm(arm, iso2)
        todo = max(merges - len(read_merges(arm, iso2)), 0)
        if not todo:
            continue
        label = ai_constants.MASKED_COUNTRY_LABEL if masked else frozen["country"]
        tin = in_tokens or (count_tokens(ai_constants.prompt_for(label, frozen["run_date"]))["tokens"]
                            + count_tokens(sm._sent_bytes(payload))["tokens"] + 50)
        total += usage.cost_usd(SCORING_MODEL, int(todo * calls_per_merge * tin),
                                int(todo * calls_per_merge * out_tokens))
    return total


def observed_call_cost() -> Optional[Dict[str, float]]:
    """Mean tokens and cost per call, and mean calls per merge, over every row made so far."""
    rows, merges = [], []
    for arm in ARMS:
        for iso2 in COUNTRIES:
            rows += read_calls(arm, iso2)
            merges += read_merges(arm, iso2)
    rows = [r for r in rows if r.get("input_tokens")]
    if not rows:
        return None
    tin = st.mean(r["input_tokens"] for r in rows)
    tout = st.mean(r["output_tokens"] for r in rows)
    return {"calls": len(rows), "in_tokens": round(tin), "out_tokens": round(tout),
            "usd_per_call": round(usage.cost_usd(SCORING_MODEL, tin, tout), 4),
            "calls_per_merge": round(st.mean(m["k"] for m in merges), 2) if merges else None,
            "extended_share": round(sum(1 for m in merges if m["extended"]) / len(merges), 2) if merges else None}


def score(arm: str, merges: int, countries: List[str], *, cap_usd: float,
          stop_usd: Optional[float] = None, project_only: bool = False) -> None:
    CALLS_DIR.mkdir(parents=True, exist_ok=True)
    MERGES_DIR.mkdir(parents=True, exist_ok=True)
    seen = observed_call_cost()
    cpm = (seen or {}).get("calls_per_merge") or 5.0
    proj = project(arm, merges, countries, calls_per_merge=cpm,
                   in_tokens=(seen or {}).get("in_tokens"), out_tokens=(seen or {}).get("out_tokens") or 950)
    print(f"[cost] {arm}: {merges} merges x {len(countries)} countries at {cpm} calls/merge "
          f"-> projected ${proj:.2f}" + (f" (observed so far: {seen})" if seen else ""))
    if project_only:
        return
    meter = usage.Meter()
    for iso2 in countries:
        frozen, payload, data, masked = _arm(arm, iso2)
        ids = [a["id"] for a in payload["articles"]]
        run_date = dt.date.fromisoformat(frozen["run_date"])
        sent_sha = _sha(sm._sent_bytes(payload))
        label = ai_constants.MASKED_COUNTRY_LABEL if masked else frozen["country"]
        done = len(read_merges(arm, iso2))
        for m in range(done, merges):
            rows_this_merge: List[Dict[str, Any]] = []

            def call(i: int) -> Dict[str, Any]:
                before = (meter.input_tokens, meter.output_tokens)
                scored = langchain_llm.score_country(
                    iso2=iso2, payload=payload, article_ids=ids, model=SCORING_MODEL, seed=SEED,
                    meter=meter, run_date=run_date, country_label=label, masked=masked,
                )
                scoring = None
                if scored["answer"]:
                    try:
                        scoring = _score(scored["answer"], data, ids, run_date,
                                         {"violations": scored["violations"], "counts": scored["counts"]})
                    except ValueError as exc:
                        scored["failed"] = f"combine: {exc}"
                row = {
                    "arm": arm, "iso2": iso2, "merge_i": m, "k_in_merge": i,
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "payload_sha256": sent_sha, "frozen_sha256": frozen["payload_sha256"],
                    "prompt_version": ai_constants.PROMPT_VERSION, "masked": masked,
                    "quote_match": validate.QUOTE_MATCH,
                    "failed": scored["failed"], "answer": scored["answer"], "raw": scored["raw"],
                    "violations": scored["violations"], "counts": scored.get("counts"),
                    "scoring": scoring,
                    "input_tokens": meter.input_tokens - before[0],
                    "output_tokens": meter.output_tokens - before[1],
                }
                _append_jsonl(CALLS_DIR / f"{arm}_{iso2}.jsonl", row)
                rows_this_merge.append(row)
                s = scoring or {}
                print(f"[score] {arm} {iso2} merge {m + 1} call {i + 1}: 12m={s.get('score_12m')} "
                      f"3m={s.get('score_3m')} ledgers={s.get('ledger_ints')} "
                      f"({'failed: ' + scored['failed'][:60] if scored['failed'] else 'ok'}) "
                      f"${meter.spend_usd:.3f} so far")
                meter.check(cap_usd)
                return scored

            result = scoring_rule.run_scoring(call)
            merged_scoring = _score(result["answer"], data, ids, run_date)
            _append_jsonl(MERGES_DIR / f"{arm}_{iso2}.jsonl", {
                "arm": arm, "iso2": iso2, "merge_i": m, "k": result["merge"]["k"],
                "extended": result["merge"]["extended"], "tied": result["merge"].get("tied", []),
                "unanimous": result["merge"].get("unanimous"), "votes": result["merge"]["votes"],
                "payload_sha256": sent_sha, "prompt_version": ai_constants.PROMPT_VERSION,
                "quote_match": validate.QUOTE_MATCH, "masked": masked,
                "answer": result["answer"], "scoring": merged_scoring,
                "input_tokens": sum(r["input_tokens"] for r in rows_this_merge),
                "output_tokens": sum(r["output_tokens"] for r in rows_this_merge),
            })
            print(f"[merge] {arm} {iso2} merge {m + 1}: k={result['merge']['k']} "
                  f"extended={result['merge']['extended']} 12m={merged_scoring['score_12m']} "
                  f"3m={merged_scoring['score_3m']} ledgers={merged_scoring['ledger_ints']}")
            if stop_usd is not None:
                seen = observed_call_cost() or {}
                remaining = project(arm, merges, countries, calls_per_merge=seen.get("calls_per_merge") or cpm,
                                    in_tokens=seen.get("in_tokens"), out_tokens=seen.get("out_tokens") or 950)
                spent_arm = sum(usage.cost_usd(SCORING_MODEL, r["input_tokens"], r["output_tokens"])
                                for c in countries for r in read_calls(arm, c))
                if spent_arm + remaining > stop_usd:
                    print(f"[cost] STOP: {arm} has spent ${spent_arm:.2f} and projects ${remaining:.2f} more, "
                          f"over the ${stop_usd:.2f} line for this arm")
                    print(meter.summary())
                    return
    print(meter.summary())


def revalidate(arm: str, quote_match: str, countries: List[str]) -> None:
    """Re-run the validator on stored raw answers under `quote_match`, re-score
    and re-merge, into arm `<arm>.<quote_match>`. No model call."""
    out_arm = f"{arm}.{quote_match}"
    previous = validate.QUOTE_MATCH
    validate.QUOTE_MATCH = quote_match
    try:
        for iso2 in countries:
            frozen, payload, data, masked = _arm(arm, iso2)
            ids = [a["id"] for a in payload["articles"]]
            run_date = dt.date.fromisoformat(frozen["run_date"])
            texts = validate.article_texts(payload)
            title_only = validate.title_only_ids(payload)
            (CALLS_DIR / f"{out_arm}_{iso2}.jsonl").unlink(missing_ok=True)
            (MERGES_DIR / f"{out_arm}_{iso2}.jsonl").unlink(missing_ok=True)
            by_merge: Dict[int, List[Dict[str, Any]]] = {}
            for r in read_calls(arm, iso2):
                by_merge.setdefault(r["merge_i"], []).append(r)
            for m, rows in sorted(by_merge.items()):
                answers = []
                for r in rows:
                    checked = validate.validate(r["raw"], article_ids=ids, run_date=run_date,
                                                texts=texts, title_only=title_only)
                    scoring = _score(checked["answer"], data, ids, run_date,
                                     {"violations": checked["violations"], "counts": checked["counts"]})
                    _append_jsonl(CALLS_DIR / f"{out_arm}_{iso2}.jsonl", {
                        **r, "arm": out_arm, "quote_match": quote_match, "answer": checked["answer"],
                        "violations": checked["violations"], "counts": checked["counts"], "scoring": scoring,
                    })
                    answers.append(checked["answer"])
                # The same rule production applies, on the same calls.
                calls_iter = iter(answers)
                result = scoring_rule.run_scoring(
                    lambda i: {"answer": next(calls_iter), "failed": None},
                    min_calls=min(3, len(answers)), vote_calls=min(5, len(answers)), max_calls=len(answers))
                merged_scoring = _score(result["answer"], data, ids, run_date)
                _append_jsonl(MERGES_DIR / f"{out_arm}_{iso2}.jsonl", {
                    "arm": out_arm, "iso2": iso2, "merge_i": m, "k": result["merge"]["k"],
                    "extended": result["merge"]["extended"], "tied": result["merge"].get("tied", []),
                    "unanimous": result["merge"].get("unanimous"), "votes": result["merge"]["votes"],
                    "payload_sha256": rows[0]["payload_sha256"], "prompt_version": ai_constants.PROMPT_VERSION,
                    "quote_match": quote_match, "masked": masked, "answer": result["answer"],
                    "scoring": merged_scoring, "revalidated_from": arm,
                    "calls_available": len(answers),
                })
            print(f"[revalidate] {out_arm} {iso2}: {len(by_merge)} merges re-merged")
    finally:
        validate.QUOTE_MATCH = previous


# --- Part 3c: identifiability -------------------------------------------------


PROBE_PROMPT = (
    "You are given an anonymised news payload about one country. Which country is this? "
    "Give your top 3 guesses with probabilities that sum to at most 1, each with the one detail "
    "that gave it away, or say there is not enough to tell."
)
PROBE_SCHEMA = {
    "name": "country_probe",
    "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "enough_to_tell": {"type": "boolean"},
            "guesses": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"country": {"type": "string"}, "probability": {"type": "number"},
                               "reason": {"type": "string"}},
                "required": ["country", "probability", "reason"]}},
        },
        "required": ["enough_to_tell", "guesses"],
    },
    "strict": True,
}


def _blank_publishers(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = copy.deepcopy(payload)
    for a in p.get("articles", []) + p.get("full_texts", []):
        if "publisher" in a:
            a["publisher"] = "[publisher]"
    return p


def probe(countries: List[str], run_label: str = "run1", *, no_publisher: bool = False) -> Dict[str, Any]:
    """Which country is this? `no_publisher` blanks the publisher names, to
    measure how much the outlets alone give away (the brief keeps them)."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI
    meter = usage.Meter()
    structured = ChatOpenAI(model=PROBE_MODEL, temperature=0.0, seed=SEED, max_retries=0,
                            api_key=os.getenv("OPENAI_API_KEY")).with_structured_output(
        schema=PROBE_SCHEMA, strict=True, include_raw=True)
    out: Dict[str, Any] = {"model": PROBE_MODEL, "run": run_label, "no_publisher": no_publisher, "countries": {}}
    for iso2 in countries:
        frozen = _load_new(iso2, "masked")
        name = frozen["country"]
        sent = _blank_publishers(frozen["payload"]) if no_publisher else frozen["payload"]
        response = structured.invoke([SystemMessage(content=PROBE_PROMPT),
                                      HumanMessage(content=sm._sent_bytes(sent))])
        meter.add_response(PROBE_MODEL, response)
        parsed = response.get("parsed") if isinstance(response, dict) else response
        guesses = sorted(parsed.get("guesses") or [], key=lambda g: -g.get("probability", 0))
        top = guesses[0] if guesses else None
        correct = bool(top) and _same_country(top["country"], name, iso2)
        verdict = ("not enough to tell" if not parsed.get("enough_to_tell") or not top
                   else "identified" if correct and top["probability"] >= 0.5
                   else "wrong" if not correct else "identified (under 0.5)")
        gave_away = (top or {}).get("reason", "")
        names_leaked = bool(gazetteer.scan(iso2, gave_away)) and "name" in {k for _, k in gazetteer.scan(iso2, gave_away)}
        out["countries"][iso2] = {"country": name, "verdict": verdict, "top": top, "guesses": guesses,
                                  "gave_away": gave_away, "name_in_reason": names_leaked}
        print(f"[probe] {iso2}: {verdict}; top guess {top and top['country']} "
              f"p={top and top['probability']}; reason: {gave_away[:120]}")
    out["spend_usd"] = round(meter.spend_usd, 4)
    existing = json.loads((OUT_DIR / "probe.json").read_text(encoding="utf-8")) if (OUT_DIR / "probe.json").exists() else {}
    existing[run_label] = out
    _write_json(OUT_DIR / "probe.json", existing)
    return out


def _same_country(guess: str, name: str, iso2: str) -> bool:
    g = guess.strip().lower()
    terms = {t.lower() for t, k in gazetteer.terms_for(iso2) if k in ("name", "alias", "code")}
    return g == name.lower() or g in terms or any(t in g for t in terms if len(t) > 3)


# --- Analysis ----------------------------------------------------------------


def _raw_nonnull(rows: List[Dict[str, Any]]) -> int:
    n = 0
    for r in rows:
        raw = r.get("raw") or {}
        for qid in ai_constants.QUESTION_IDS:
            if ((raw.get(qid) or {}).get("level")) is not None:
                n += 1
    return n


def _count(rows: List[Dict[str, Any]], key: str) -> int:
    return sum((r.get("counts") or {}).get(key, 0) for r in rows)


def _modal_levels(merges: List[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    out = {}
    for qid in ai_constants.QUESTION_IDS:
        levels = [m["answer"]["questions"][qid]["level"] for m in merges]
        c = Counter(levels)
        out[qid] = c.most_common(1)[0][0] if c else None
    return out


def _median(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return st.median(vals) if vals else None


def analyse() -> Dict[str, Any]:
    out: Dict[str, Any] = {"prompt_version": ai_constants.PROMPT_VERSION,
                           "questions_version": ai_constants.QUESTIONS_VERSION,
                           "quote_match": validate.QUOTE_MATCH}
    lines: List[str] = []

    # Part 2.
    if (OUT_DIR / "selection_check.json").exists():
        sel = json.loads((OUT_DIR / "selection_check.json").read_text(encoding="utf-8"))
        out["selection_check"] = sel
        bad = [c for c, r in sel["countries"].items()
               if r["new"]["multi_article_stories"] or r["new"]["title_only_sent"] or r["new"]["t5_sent"] > 2]
        lines.append(f"Selection: {'PASS' if not bad else 'FAIL ' + str(bad)} — "
                     + "; ".join(f"{c}: stories {r['old']['multi_article_stories']}->{r['new']['multi_article_stories']}, "
                                 f"title-only {r['old']['title_only_sent']}->{r['new']['title_only_sent']}, "
                                 f"T5 {r['old']['t5_sent']}->{r['new']['t5_sent']}, sent {r['old']['articles_sent']}->"
                                 f"{r['new']['articles_sent']}" for c, r in sel["countries"].items()))
        leaks = {c: r["mask"].get("payload_leaks", 0) for c, r in sel["countries"].items()}
        lines.append(f"Leaks in the frozen payloads: {'PASS' if not any(leaks.values()) else 'FAIL'} {leaks}")

    # Fix 3: the quote rule, over every call made.
    all_rows = [r for arm in ARMS for c in COUNTRIES for r in read_calls(arm, c) if r.get("raw")]
    if all_rows:
        nonnull = _raw_nonnull(all_rows)
        qn = _count(all_rows, "levels_nulled_quote_not_found")
        rate = qn / nonnull if nonnull else 0.0
        decision = ("keep the rule" if rate <= 0.15 else
                    "15–30%: inspect the failures; switch to ordered_90 if near-misses dominate" if rate <= 0.30
                    else "over 30%: report-only")
        examples = [{"arm": r["arm"], "iso2": r["iso2"], "field": v["field"], "quote": v["raw"]}
                    for r in all_rows for v in r["violations"] if v["problem"] == "quote not found"][:12]
        by_arm = {}
        for arm in ARMS:
            rows = [r for r in all_rows if r["arm"] == arm]
            if rows:
                nn = _raw_nonnull(rows)
                by_arm[arm] = {"calls": len(rows), "nonnull_raw_levels": nn,
                               "quote_nulled": _count(rows, "levels_nulled_quote_not_found"),
                               "rate": round(_count(rows, "levels_nulled_quote_not_found") / nn, 3) if nn else None,
                               "title_only_nulled": _count(rows, "levels_nulled_title_only"),
                               "no_evidence_nulled": _count(rows, "levels_nulled_no_evidence")}
        out["quote_rule"] = {"nonnull_raw_levels": nonnull, "quote_nulled": qn, "rate": round(rate, 3),
                             "decision": decision, "by_arm": by_arm, "examples": examples}
        lines.append(f"Quote rule (Fix 3): {qn} of {nonnull} non-null levels nulled ({rate:.1%}) -> {decision}")
        lines.append(f"Title-only tripwire (Fix 2): {_count(all_rows, 'levels_nulled_title_only')} levels nulled "
                     f"({'PASS' if _count(all_rows, 'levels_nulled_title_only') == 0 else 'LOOK'})")

    # Fix 4: no-evidence nulls before and after, per call.
    before_rows = [r for c in COUNTRIES for r in _read_jsonl(OLD_CALLS_DIR / f"full_{c}.jsonl")]
    after_rows = [r for c in COUNTRIES for r in read_calls("old_named", c)]
    if before_rows and after_rows:
        b = _count(before_rows, "levels_nulled_no_evidence") / len(before_rows)
        a = _count(after_rows, "levels_nulled_no_evidence") / len(after_rows)
        per_country = {c: {"before": round(_count(_read_jsonl(OLD_CALLS_DIR / f"full_{c}.jsonl"), "levels_nulled_no_evidence")
                                           / max(len(_read_jsonl(OLD_CALLS_DIR / f"full_{c}.jsonl")), 1), 2),
                           "after": round(_count(read_calls("old_named", c), "levels_nulled_no_evidence")
                                          / max(len(read_calls("old_named", c)), 1), 2)} for c in COUNTRIES}
        out["no_evidence"] = {"before_per_call": round(b, 2), "after_per_call": round(a, 2),
                              "before_calls": len(before_rows), "after_calls": len(after_rows),
                              "per_country": per_country}
        lines.append(f"No-evidence nulls (Fix 4): {b:.2f} per call before (step 5d, {len(before_rows)} calls) -> "
                     f"{a:.2f} after (old payloads, new instrument, {len(after_rows)} calls)")

    # Fix 7: extension.
    ext = {}
    for arm in ARMS:
        ms = [m for c in COUNTRIES for m in read_merges(arm, c)]
        if ms:
            ext[arm] = {"merges": len(ms), "extended": sum(1 for m in ms if m["extended"]),
                        "k_counts": dict(Counter(m["k"] for m in ms)),
                        "tied_at_end": sum(1 for m in ms if m.get("tied"))}
    out["extension"] = ext
    if ext:
        lines.append("Split votes (Fix 7): " + "; ".join(
            f"{arm} {e['extended']}/{e['merges']} merges extended, k {e['k_counts']}" for arm, e in ext.items()))

    # 3a: old payloads, new instrument vs step 5d.
    if OLD_ANALYSIS_PATH.exists() and any(read_merges("old_named", c) for c in COUNTRIES):
        old = json.loads(OLD_ANALYSIS_PATH.read_text(encoding="utf-8"))["m1"]["per_country"]
        comp = {}
        for c in COUNTRIES:
            ms = read_merges("old_named", c)
            if not ms:
                continue
            new_med = _median([m["scoring"]["score_12m"] for m in ms])
            old_med = old.get(c, {}).get("score_12m", {}).get("median")
            new_modal = _modal_levels(ms)
            old_modal = {q: v.get("modal") for q, v in old.get(c, {}).get("questions", {}).items()}
            changed = []
            rows = read_calls("old_named", c)
            for qid in ai_constants.QUESTION_IDS:
                if new_modal.get(qid) != old_modal.get(qid):
                    quote_nulls = sum(1 for r in rows for v in r["violations"]
                                      if v["field"] == f"{qid}.quote")
                    raw_levels = [((r.get("raw") or {}).get(qid) or {}).get("level") for r in rows]
                    raw_null_share = sum(v is None for v in raw_levels) / max(len(raw_levels), 1)
                    votes = [m["votes"].get(qid) for m in ms]
                    if quote_nulls and new_modal.get(qid) is None:
                        why = "quote rule"
                    elif old_modal.get(qid) is not None and raw_null_share >= 0.5:
                        why = "model now answers null (date sentence / use 0 only for shown normal functioning)"
                    elif any(len(set(v)) > 1 for v in votes if v):
                        why = "vote"
                    else:
                        why = "level moved on the same evidence"
                    changed.append({"question": qid, "step5d": old_modal.get(qid), "now": new_modal.get(qid),
                                    "why": why, "quote_nulls": quote_nulls,
                                    "raw_null_share": round(raw_null_share, 2),
                                    "quote": next((m["answer"]["questions"][qid]["quote"] for m in ms
                                                   if m["answer"]["questions"][qid]["level"] == new_modal.get(qid)), "")})
            comp[c] = {"step5d_median": old_med, "now_median": new_med,
                       "delta": (new_med - old_med) if (new_med is not None and old_med is not None) else None,
                       "merges": len(ms), "changed_questions": changed}
        out["old_vs_step5d"] = comp
        lines.append("3a old payloads, new instrument vs step 5d: " + "; ".join(
            f"{c} {v['step5d_median']}->{v['now_median']} ({len(v['changed_questions'])} q changed)" for c, v in comp.items()))

    # 3b: the reference run.
    masked = {c: read_merges("new_masked", c) for c in COUNTRIES}
    if any(masked.values()):
        m1 = {}
        for c, ms in masked.items():
            if not ms:
                continue
            s12 = [m["scoring"]["score_12m"] for m in ms]
            s3 = [m["scoring"]["score_3m"] for m in ms]
            m1[c] = {"merges": len(ms), "score_12m": sm._stats(s12), "score_3m": sm._stats(s3),
                     "ledgers": {led: sm._stats([m["scoring"]["ledger_ints"][led] for m in ms]) for led in sm.LEDGER_SCORES},
                     "answered": [m["scoring"]["news_scores"] and sum(
                         1 for q in m["answer"]["questions"].values() if q["level"] is not None) for m in ms],
                     "questions": sm._question_agreement([m["scoring"] for m in ms]),
                     "k": [m["k"] for m in ms]}
        worst = max((v["score_12m"]["sd"] for v in m1.values() if v["score_12m"]["n"] > 1), default=0.0)
        flips = {c: [q for q, a in v["questions"].items() if a["flips"]] for c, v in m1.items()}
        out["m1"] = {"per_country": m1, "worst_sd": worst, "pass": worst <= 1.0, "flipping_questions": flips}
        lines.append(f"M1 (masked, worst score_12m SD over merges): {worst} -> {'PASS' if worst <= 1.0 else 'FAIL'}; "
                     f"flipping: { {c: f for c, f in flips.items() if f} }")
        order = sorted(m1, key=lambda c: -m1[c]["score_12m"]["median"])
        pt_from_bottom = (len(order) - order.index("PT")) if "PT" in order else None
        fv = bool(order) and order[0] == "RU" and pt_from_bottom in (1, 2)
        out["face_validity"] = {"order": [(c, m1[c]["score_12m"]["median"]) for c in order],
                                "ru_highest": bool(order) and order[0] == "RU",
                                "pt_from_bottom": pt_from_bottom, "pass": fv}
        lines.append(f"Face validity (masked medians): {[(c, m1[c]['score_12m']['median']) for c in order]} -> "
                     f"{'PASS' if fv else 'FAIL'}")
        gaps, untraced, dropped = [], [], Counter()
        for c, ms in masked.items():
            for m in ms:
                gap = m["scoring"]["score_12m"] - m["scoring"]["score_3m"]
                if gap:
                    ev = m["scoring"]["events"]["valid"]
                    ok = bool(ev)
                    gaps.append({"iso2": c, "merge": m["merge_i"], "gap": gap, "events": ev})
                    if not ok:
                        untraced.append({"iso2": c, "merge": m["merge_i"], "gap": gap})
            for r in read_calls("new_masked", c):
                for v in r["violations"]:
                    if v["field"].startswith("scheduled_events"):
                        dropped[v["problem"]] += 1
        out["m4"] = {"gaps": gaps, "untraced": untraced, "dropped_events": dict(dropped), "pass": not untraced}
        lines.append(f"M4 (masked): {len(gaps)} merge(s) with a 3m gap, {len(untraced)} untraced -> "
                     f"{'PASS' if not untraced else 'FAIL'}; events dropped {dict(dropped)}")

        # Masking cost.
        named = {c: read_merges("new_named", c) for c in COUNTRIES}
        if any(named.values()):
            cost = {}
            for c in COUNTRIES:
                if not masked[c] or not named[c]:
                    continue
                mm = _median([m["scoring"]["score_12m"] for m in masked[c]])
                nm = _median([m["scoring"]["score_12m"] for m in named[c]])
                ml, nl = _modal_levels(masked[c]), _modal_levels(named[c])
                diffs = []
                for qid in ai_constants.QUESTION_IDS:
                    if ml[qid] != nl[qid]:
                        mq = next((m["answer"]["questions"][qid] for m in masked[c]
                                   if m["answer"]["questions"][qid]["level"] == ml[qid]), {})
                        nq = next((m["answer"]["questions"][qid] for m in named[c]
                                   if m["answer"]["questions"][qid]["level"] == nl[qid]), {})
                        diffs.append({"question": qid, "masked": ml[qid], "named": nl[qid],
                                      "masked_quote": mq.get("quote"), "masked_evidence": mq.get("evidence"),
                                      "named_quote": nq.get("quote"), "named_evidence": nq.get("evidence")})
                cost[c] = {"masked_median": mm, "named_median": nm, "masked_minus_named": mm - nm,
                           "named_merges": len(named[c]), "masked_merges": len(masked[c]), "differing_questions": diffs}
            over = [c for c, v in cost.items() if abs(v["masked_minus_named"]) > 5]
            out["masking_cost"] = {"per_country": cost, "over_5": over, "adopt": not over}
            lines.append("Masking cost (median masked − median named): "
                         + ", ".join(f"{c} {v['masked_minus_named']:+.1f} ({len(v['differing_questions'])} q differ)"
                                     for c, v in cost.items())
                         + f" -> {'ADOPT (all within 5)' if not over else 'STOP: ' + str(over)}")

    if (OUT_DIR / "probe.json").exists():
        pr = json.loads((OUT_DIR / "probe.json").read_text(encoding="utf-8"))
        out["probe"] = pr
        for run, res in pr.items():
            lines.append(f"Identifiability ({run}): " + "; ".join(
                f"{c} {v['verdict']}" + (f" (names in reason)" if v.get("name_in_reason") else "")
                for c, v in res["countries"].items()))

    seen = observed_call_cost()
    spend = {}
    for arm in ARMS:
        rows = [r for c in COUNTRIES for r in read_calls(arm, c)]
        if rows:
            spend[arm] = {"calls": len(rows),
                          "usd": round(sum(usage.cost_usd(SCORING_MODEL, r["input_tokens"], r["output_tokens"]) for r in rows), 4)}
    out["spend"] = {"by_arm": spend, "scorer_total_usd": round(sum(v["usd"] for v in spend.values()), 4),
                    "observed_call": seen}
    lines.append(f"Scorer spend: {out['spend']['scorer_total_usd']} USD over {sum(v['calls'] for v in spend.values())} calls; "
                 f"per call {seen and seen['usd_per_call']}, calls per merge {seen and seen['calls_per_merge']}")

    out["lines"] = lines
    _write_json(OUT_DIR / "analysis.json", out)
    print("\n".join(lines))
    return out


def reference(countries: List[str]) -> None:
    for iso2 in countries:
        ms = read_merges("new_masked", iso2)
        if not ms:
            print(f"[reference] {iso2}: no masked merge")
            continue
        first = ms[0]
        frozen = _load_new(iso2, "masked")
        _write_json(OUT_DIR / f"reference_{iso2}.json", {
            "iso2": iso2, "country": frozen["country"], "run_date": frozen["run_date"],
            "payload_file": f"backend/measurements/step5e/payload_{iso2}.json",
            "payload_sha256": frozen["payload_sha256"], "prompt_version": ai_constants.PROMPT_VERSION,
            "questions_version": ai_constants.QUESTIONS_VERSION, "scoring_model": SCORING_MODEL,
            "seed": SEED, "mode": "masked", "quote_match": first.get("quote_match"),
            "merge_rule": "3 unanimous, else plurality over 5, ties to 7 (scoring.run_scoring)",
            "k": first["k"], "extended": first["extended"], "votes": first["votes"],
            "answer": first["answer"], "scoring": first["scoring"],
            "all_merges_score_12m": [m["scoring"]["score_12m"] for m in ms],
        })
        print(f"[reference] {iso2}: merge 1 of {len(ms)}, k={first['k']}, score_12m={first['scoring']['score_12m']}")


# --- CLI ---------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("select-check")
    p.add_argument("--countries", nargs="*", default=COUNTRIES)
    p.add_argument("--cap-usd", type=float, default=2.0)
    p = sub.add_parser("score")
    p.add_argument("--arm", choices=ARMS, required=True)
    p.add_argument("--merges", type=int, required=True)
    p.add_argument("--countries", nargs="*", default=COUNTRIES)
    p.add_argument("--cap-usd", type=float, default=18.0)
    p.add_argument("--stop-usd", type=float, default=None)
    p.add_argument("--project-only", action="store_true")
    p = sub.add_parser("revalidate")
    p.add_argument("--arm", required=True)
    p.add_argument("--quote-match", choices=("exact", "ordered_90", "report"), required=True)
    p.add_argument("--countries", nargs="*", default=COUNTRIES)
    p = sub.add_parser("probe")
    p.add_argument("--countries", nargs="*", default=COUNTRIES)
    p.add_argument("--run", default="run1")
    p.add_argument("--no-publisher", action="store_true")
    sub.add_parser("analyse")
    p = sub.add_parser("reference")
    p.add_argument("--countries", nargs="*", default=COUNTRIES)
    args = ap.parse_args(argv)

    if args.cmd == "select-check":
        select_check([c.upper() for c in args.countries], args.cap_usd)
    elif args.cmd == "score":
        score(args.arm, args.merges, [c.upper() for c in args.countries], cap_usd=args.cap_usd,
              stop_usd=args.stop_usd, project_only=args.project_only)
    elif args.cmd == "revalidate":
        revalidate(args.arm, args.quote_match, [c.upper() for c in args.countries])
    elif args.cmd == "probe":
        probe([c.upper() for c in args.countries], args.run, no_publisher=args.no_publisher)
    elif args.cmd == "analyse":
        analyse()
    elif args.cmd == "reference":
        reference([c.upper() for c in args.countries])


if __name__ == "__main__":
    main()
