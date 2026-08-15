"""Rebuild one stored snapshot from its manifest and diff the two.

`input_manifest`'s first real consumer. It has been written on every snapshot
since provenance landed, and its whole promise — "this row can be reproduced, or
found to be irreproducible" — had never been tested against a stored row.

Everything is re-derived from caches, so a rebuild costs nothing and makes no
model call: the article set and its order come from `snapshot_select` over the
same window, the digests from `history_digest_cache`, the rewritten full texts
from `history_rewrite_cache`, and `macro_vintages` is a pure function of `as_of`.

The rewrite cache is what makes this a real check. Before it existed the two or
three articles in `fulltext_ids` had their bodies replaced by a model call whose
output was kept nowhere, so `content_sha256` hashed prose that no longer existed
and a rebuild could only ever report a mismatch — on precisely the articles the
scorer weighted most heavily. This script had to skip them entirely.

Now a full-text mismatch means a cache miss, which is a real finding: that row
cannot be rebuilt. Rows written before the cache existed will always report it.

    python -m backend.util.tools.rebuild_snapshot PT 2019-06-03
"""

import argparse
import datetime
import json
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / "backend" / ".env")

from backend.util import constants  # noqa: E402
from backend.llm import payload as dr  # noqa: E402
from backend.util import pipeline, provenance  # noqa: E402
from backend.llm import client as ai_client, digest_engine, langchain_llm  # noqa: E402
from backend.data_upsert import data_push  # noqa: E402
from backend.data_upsert import store  # noqa: E402
from backend.news_fetching import snapshot_select  # noqa: E402
from backend.util import config  # noqa: E402
from backend.llm import gazetteer, rewrite  # noqa: E402


def stored_manifest(iso2: str, as_of: datetime.date) -> dict:
    with data_push._transaction() as cur:
        cur.execute("SELECT input_manifest FROM risk_snapshot "
                    "WHERE country_iso2 = %s AND as_of = %s", (iso2, as_of))
        row = cur.fetchone()
    return (row or [None])[0] or {}


def rebuild(iso2: str, as_of: datetime.date, items: list = None) -> dict:
    """Re-assemble the snapshot on the same code path, without scoring it.

    ``items`` lets the caller hand in the selection it already checked for cache
    coverage, so the row that was proved free is the row that gets rebuilt.
    """
    if items is None:
        items = snapshot_select.select(iso2, as_of)
        for i, it in enumerate(items, start=1):
            it["id"] = f"a{i}"
    scored = rewrite.mask_items(items, iso2)
    # Cache-served, so this costs nothing and is the point: a digest that had to
    # be regenerated would prove the cache broken rather than the row rebuilt.
    scored = digest_engine.digest_articles(
        scored, country_display=langchain_llm.MASKED_COUNTRY_LABEL, iso2=iso2,
        as_of=as_of, masked=True, content_cache=store)
    fulltext_ids = digest_engine.select_fulltext_ids(scored)
    # Cache-served, like the digests. Before `history_rewrite_cache` existed this
    # step had to be skipped entirely — the rewrite is a model call whose output
    # was kept nowhere, so re-running it wrote a different sentence and there was
    # nothing to compare against. Now it is a lookup, and a *miss* here is the
    # finding: it means the row cannot be rebuilt, which is exactly what this
    # script is for.
    pipeline._rewrite_fulltext(scored, fulltext_ids, iso2, cache=store)
    evidence = dr.build_evidence_payload(
        iso2, as_of=as_of,
        series=data_push.read_indicator_series(iso2),
        fx_regimes=constants.FX_REGIMES, elections=constants.ELECTIONS,
        vintage_as_of=as_of)
    # Two payloads, and the manifest wants the other one. `macro_vintages` reads
    # `_meta` and `indicators`, which live on the *panel* payload — the evidence
    # payload has neither, so passing it here made every block of vintage
    # metadata come back None and the diff report `DIFFERS` on every row ever
    # written. A verifier that always fails is not a verifier, and it fails in
    # the direction that reads as "the store is broken".
    #
    # Built with `pipeline`'s own constants and its one `generated_at` pin, so
    # the rebuild reads the same panel the scorer did rather than one assembled
    # to look similar.
    panel = dr.prepare_llm_payload_pretty(
        country_iso=iso2, indicators=constants.ALL_INDICATORS,
        since=pipeline._PAYLOAD_SINCE_YEAR,
        lookback=pipeline._PAYLOAD_LOOKBACK_YEARS,
        deltas=pipeline._PAYLOAD_DELTA_HORIZONS)
    panel.setdefault("_meta", {})["generated_at"] = as_of.isoformat()
    return provenance.build_input_manifest(
        items=scored, prompt_entries=langchain_llm.prompt_entries(scored),
        fulltext_ids=fulltext_ids, payload=panel,
        model_id=ai_client.MODEL_NAME, prompt_version=None, policy_version=None,
        seed=ai_client.SEED,
        masking={"scoring_mode": "masked",
                 "mask_map_version": gazetteer.MASK_MAP_VERSION,
                 "sweep_version": rewrite.SWEEP_VERSION,
                 "mask_integrity_status": "clean",
                 "structural_fields": len(evidence.get("structural") or {}),
                 "identifiability": None})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("iso2", nargs="?", default="PT")
    parser.add_argument("as_of", nargs="?", type=datetime.date.fromisoformat,
                        default=datetime.date.today(), metavar="YYYY-MM-DD")
    args = parser.parse_args()
    iso2, as_of = args.iso2.upper(), args.as_of

    old = stored_manifest(iso2, as_of)
    if not old:
        print(f"no stored manifest for {iso2} {as_of}")
        return

    # Refuse before spending, rather than reporting a mismatch after. A row whose
    # digests are not in the cache would be re-digested at full price, producing
    # prose the stored row never contained and reporting a drift the rebuild had
    # just created. Costing money to manufacture a failure is worse than
    # declining to check.
    #
    # Asked of the cache rather than inferred from a version stamp. The masked
    # cache key is `masked:{mask_map_version}:{sweep_version}`, and this guard
    # used to compare the sweep alone: a gazetteer bump invalidated every digest
    # while the check reported clean, and a row with no sweep recorded passed
    # unconditionally. Coverage cannot drift from the key because it is the key.
    items = snapshot_select.select(iso2, as_of)
    for i, it in enumerate(items, start=1):
        it["id"] = f"a{i}"
    missing = digest_engine.digest_coverage(
        rewrite.mask_items(items, iso2), iso2=iso2, as_of=as_of,
        masked=True, content_cache=store)
    if missing:
        stored = old.get("masking") or {}
        print(f"\n{iso2} {as_of}: {len(missing)} of {len(items)} digests are not "
              f"in the cache, so a rebuild would call the model {len(missing)} "
              f"time(s) and compare the row against prose it never had.")
        print(f"  stored mask map {stored.get('mask_map_version') or '(none)'} / "
              f"sweep {stored.get('sweep_version') or '(none)'}")
        print(f"  this tree       {gazetteer.MASK_MAP_VERSION} / "
              f"{rewrite.SWEEP_VERSION}")
        print("  Masked digests are keyed on both, so a row written under either "
              "an older\n  gazetteer or an unrecorded sweep is not reproducible "
              "here by construction.\n  Rebuild a row written by the current tree.")
        return

    new = rebuild(iso2, as_of, items=items)

    print(f"\n=== rebuild {iso2} {as_of} ===")
    old_articles = {a.get("id"): a for a in old.get("articles") or []}
    new_articles = {a.get("id"): a for a in new.get("articles") or []}
    print(f"  articles: stored {len(old_articles)}, rebuilt {len(new_articles)}, "
          f"same ids: {set(old_articles) == set(new_articles)}")

    exact = drifted_full = drifted_other = 0
    for aid in sorted(set(old_articles) | set(new_articles),
                      key=lambda s: int(str(s).lstrip("a") or 0)):
        a, b = old_articles.get(aid, {}), new_articles.get(aid, {})
        same = (a.get("content_sha256") == b.get("content_sha256")
                and a.get("prompt_text_sha256") == b.get("prompt_text_sha256"))
        if same:
            exact += 1
            continue
        if a.get("in_fulltext") or b.get("in_fulltext"):
            drifted_full += 1
            print(f"  {aid}: differs — IN FULLTEXT. Since the rewrite cache "
                  f"exists this is a cache MISS, not an expected drift: the "
                  f"rewritten body for this article is gone and the row cannot "
                  f"be rebuilt.")
        else:
            drifted_other += 1
            print(f"  {aid}: differs — NOT in full text (this one is a real "
                  f"reproducibility failure)")
            print(f"      stored  content={a.get('content_sha256')} "
                  f"prompt={a.get('prompt_text_sha256')}")
            print(f"      rebuilt content={b.get('content_sha256')} "
                  f"prompt={b.get('prompt_text_sha256')}")

    print(f"\n  exact: {exact}   full-text drift (expected): {drifted_full}   "
          f"unexplained drift: {drifted_other}")

    for block in ("macro_vintages", "stage1"):
        match = json.dumps(old.get(block), sort_keys=True) == \
            json.dumps(new.get(block), sort_keys=True)
        print(f"  {block:<16} {'identical' if match else 'DIFFERS'}")
        if not match:
            print(f"      stored : {json.dumps(old.get(block), sort_keys=True)[:300]}")
            print(f"      rebuilt: {json.dumps(new.get(block), sort_keys=True)[:300]}")

    old_mask = old.get("masking") or {}
    new_mask = new.get("masking") or {}
    print(f"  mask_map_version  stored {old_mask.get('mask_map_version')} "
          f"-> rebuilt {new_mask.get('mask_map_version')}")
    print(f"  sweep_version     stored {old_mask.get('sweep_version')} "
          f"-> rebuilt {new_mask.get('sweep_version')}")
    print(f"  payload_version   stored {old.get('payload_version')} "
          f"-> rebuilt {new.get('payload_version')}")

    if drifted_other:
        print("\n  VERDICT: not reproducible — the drift is outside the full-text "
              "block and needs explaining before the pilot.")
    elif drifted_full:
        print("\n  VERDICT: not reproducible — the rewritten bodies for "
              f"{drifted_full} full-text article(s) are missing from "
              "`history_rewrite_cache`. Rows written before that cache existed "
              "will always report this; rows written after it should not.")
    else:
        print("\n  VERDICT: byte-for-byte.")


if __name__ == "__main__":
    main()
