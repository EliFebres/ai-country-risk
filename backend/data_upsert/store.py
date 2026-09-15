"""The model-output cache: stage-1 digests keyed on the article's content.

`llm_artifact` holds one row per (content hash, kind, version, mode). The live
run reads and writes `kind = 'digest'` through `digest_engine`. The table also
keeps `rewrite` and `context` rows from the retired historical programme, which
nothing reads (`docs/historical-ratings-postmortem.md`).

The DDL lives in :mod:`backend.data_upsert.schema`, not here.
"""

from typing import Any, Dict, Sequence

import psycopg2.extras as extras

from backend.data_upsert import data_push

# The project's one connect/commit/rollback/close helper, shared rather than
# duplicated.
_transaction = data_push._transaction


# Keyed on content, so an article still in the news window next week is not
# paid for twice: its digest is identical in both snapshots.
#
# `mode` is in the key because the masked and named digests of one article are
# genuinely different texts, and must never be served for each other. `kind`
# separates a digest from a full-text rewrite, which are the same shape of fact
# — this model, this version, this text, this output — and used to be two
# tables saying it twice.

def read_digest_cache(hashes: Sequence[str], digest_model: str,
                      mode: str) -> Dict[str, Dict[str, Any]]:
    """Cached digests for these content hashes, keyed by hash.

    A miss is an absent key, never a null row: the caller re-digests whatever is
    missing, which is also what happens the first time a mask map changes and
    every masked hash is new.
    """
    if not hashes:
        return {}
    with _transaction() as cur:
        cur.execute(
            """
            SELECT content_sha256, payload, stage1_severity
              FROM llm_artifact
             WHERE kind = 'digest' AND content_sha256 = ANY(%s)
               AND version = %s AND mode = %s
            """,
            (list(hashes), digest_model, mode),
        )
        return {r[0]: {"digest": r[1], "stage1_severity": r[2]} for r in cur.fetchall()}


def write_digest_cache(rows: Sequence[Dict[str, Any]], digest_model: str,
                       mode: str) -> int:
    """Cache digests by content hash.

    Args:
        rows: dicts with ``content_sha256``, ``digest`` and ``stage1_severity``.
            Rows without a hash or without a digest are dropped — a failed
            digest must be retried next time, not cached as a failure.

    Returns:
        How many rows were written.
    """
    values = [
        (r["content_sha256"], "digest", digest_model, mode,
         data_push._json_or_none(r["digest"]), r.get("stage1_severity"))
        for r in rows
        if r.get("content_sha256") and isinstance(r.get("digest"), dict)
    ]
    if not values:
        return 0
    with _transaction() as cur:
        extras.execute_values(
            cur,
            """
            INSERT INTO llm_artifact
              (content_sha256, kind, version, mode, payload, stage1_severity)
            VALUES %s
            ON CONFLICT (content_sha256, kind, version, mode) DO NOTHING
            """,
            values,
            page_size=200,
        )
    return len(values)
