"""What the model saw: the input manifest stored with every risk snapshot.

A stored score used to be unreproducible. The row said 0.62 and nothing else —
not which articles the scorer read, not whether it read them in full, not which
vintage of the macro panel it was reasoning over. Re-running the same day a week
later could give a different number for reasons nothing recorded.

This module builds the record that closes that gap: per article, a hash of the
body we held and a hash of the exact text the prompt carried, plus the macro
panel's vintage and the model/prompt/policy stamps. Hashes rather than copies —
the point is to *detect* that an input changed, and the article bodies
themselves are already in ``article_digest``.

Everything here is a pure function: no database, no network, no clock. Times and
versions arrive as arguments, so a manifest can be rebuilt for a historical
``as_of`` and unit-tested without either dependency. That purity is also why the
content rule below is spelled out rather than imported from ``digest_engine``.

Nothing here may raise into the pipeline: provenance is metadata, not the
product. Malformed inputs degrade to None fields, and the one caller
(``pipeline._process_country``) wraps the whole assembly anyway.
"""

import hashlib
import json
import os
from typing import Any, Dict, Iterable, List, Optional

# Stamped into every manifest. Bump when the manifest's *shape* changes, so a
# reader can tell a missing field from a field that never existed.
_SCHEMA_VERSION = 1

# Which evidence contract the model was scored against — the set of indicators
# the payload can carry. Bump on any change to `INDICATOR_REGISTRY`'s membership
# or to what a ledger section may contain.
#
# The prompt and the mask map were already versioned; this was the third thing
# that changes what the model sees and the only one a reader could not date. Two
# scores built on different indicator sets are not comparable, and without this
# nothing in the row says which set it was.
#
#   p1  the registry as it stood through the masked cutover
#   p2  adds the IMF WEO block — aggregate real GDP growth, gross debt, net
#       lending and the current account, all edition-vintaged
#   p3-context  adds the trailing-context block: one masked paragraph per
#       calendar quarter for the four completed quarters before the live window
PAYLOAD_VERSION = "p2"

# The variants this build knows about, and the environment that selects one.
# Unset is `p2`, which is byte-identical to the daily run — the same contract
# `client.scoring_model()` holds for the model, and for the same reason: an A/B
# must not be able to change what the pilot does by existing.
PAYLOAD_VARIANTS = ("p2", "p3-context")


def payload_variant() -> str:
    """Which evidence contract this process builds. Defaults to today's."""
    variant = os.getenv("PAYLOAD_VARIANT") or PAYLOAD_VERSION
    if variant not in PAYLOAD_VARIANTS:
        raise ValueError(f"PAYLOAD_VARIANT must be one of {PAYLOAD_VARIANTS}, "
                         f"got {variant!r}")
    return variant


def payload_version() -> str:
    """The version stamped on a row, environment override included.

    Read rather than imported by anything that records a version, so a manifest
    and a freeze say what the run actually built and not what the file says.
    """
    return payload_variant()

# How the macro panel this snapshot consumed relates to real point-in-time data.
# "as-published-latest" means: latest published values, silently revised by the
# World Bank over time. Phase B's first-release panel writes "first-release"
# here, and Phase E can then filter out ratings built on revised numbers — a
# literal string a query can test, rather than a comment nobody can.
_VINTAGE_SCHEME = "as-published-latest"


def text_sha256(text: Optional[str]) -> Optional[str]:
    """SHA-256 hex digest of ``text``, UTF-8 encoded.

    Args:
        text: the string to hash, or None.

    Returns:
        The hex digest, or None for None/empty input — "no text" and "the hash
        of the empty string" are different facts, and only the first is true.
    """
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> Optional[str]:
    """Serialize ``value`` for hashing, key order normalized.

    ``sort_keys`` matters: two runs that built the same prompt entry from
    differently-ordered dicts must hash identically, or every re-run would look
    like the inputs changed.
    """
    if value is None:
        return None
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def article_manifest_entry(item: Dict,
                           prompt_entry: Optional[Dict] = None,
                           *,
                           in_fulltext: bool = False) -> Dict[str, Any]:
    """One article's provenance record.

    Two hashes, because "what we had" and "what the model read" are different
    questions and the gap between them is the interesting one:
    ``content_sha256`` proves which body was available, ``prompt_text_sha256``
    proves which bytes reached the prompt.

    Args:
        item: an article dict at LLM time — carrying ``id`` (assigned in
            ``pipeline._process_country``), ``link``, ``source``, ``published``,
            and a body under ``content`` and/or ``text``.
        prompt_entry: this article's entry from
            ``langchain_llm.prompt_entries``, or None if the prompt never
            carried it. Its presence *is* ``in_prompt`` — deriving the flag from
            the hashed object means the two can never disagree.
        in_fulltext: whether the prompt carried the article's full text as well
            as its digest (``digest_engine.select_fulltext_ids``).

    Returns:
        A JSON-serializable dict. Never raises on a malformed item: missing
        fields come back as None.
    """
    # `content` (fallback scraper) or `text` (trafilatura), whichever exists.
    # `digest_engine.article_input_text` prefers the *longer* of the two, so on
    # an article carrying both this hash may cover the shorter body. Accepted:
    # importing that module would pull langchain and psycopg2 into a module
    # whose whole value is being dependency-free.
    body = item.get("content") or item.get("text") or ""
    if not isinstance(body, str):
        body = ""

    return {
        "id": item.get("id") or None,
        # `link` rather than `publisher_link`: `resolve_and_enrich` overwrites
        # `link` with the resolved publisher URL, and it is the same value
        # `risk_snapshot_article.url` stores — so the manifest joins to the
        # article rows. `publisher_link` is the pre-resolution fallback.
        "url": item.get("link") or item.get("publisher_link") or None,
        "source": item.get("source") or None,
        # An NYT archive row is a headline and two sentences; a Guardian row is a
        # body. Both count as one article everywhere else, so without this the
        # only record of how thin a snapshot's evidence was is the ration log of
        # the run that built it. `reports.evidence_texture` exists to answer
        # "did the divergence track the abstract share" and was reading a key
        # nothing wrote, so it answered 0.000 for every country-year.
        "tier": item.get("tier") or None,
        # The item key is `published`; `published_at` only exists on output rows.
        "published_at": item.get("published") or None,
        "content_sha256": text_sha256(body),
        "prompt_text_sha256": text_sha256(_canonical_json(prompt_entry)),
        "content_chars": len(body),
        "in_prompt": prompt_entry is not None,
        "in_fulltext": bool(in_fulltext),
    }


def build_article_manifest(items: List[Dict],
                           prompt_entries: Optional[List[Dict]] = None,
                           fulltext_ids: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """A manifest entry for every fetched article, in fetch order.

    Every article is recorded, not only the ones the model read: "we had this
    and did not use it" is itself a fact a later model may want.

    Args:
        items: the country's article dicts at LLM time.
        prompt_entries: ``langchain_llm.prompt_entries(items)`` — matched to
            items by id.
        fulltext_ids: ids whose full text the prompt carried.

    Returns:
        One entry per dict in ``items``; non-dict entries are skipped rather
        than raising.
    """
    by_id = {e.get("id"): e for e in (prompt_entries or []) if isinstance(e, dict) and e.get("id")}
    full = set(fulltext_ids or ())
    return [
        article_manifest_entry(it, by_id.get(it.get("id")), in_fulltext=it.get("id") in full)
        for it in (items or []) if isinstance(it, dict)
    ]


def stage1_health(items: List[Dict]) -> Dict[str, Any]:
    """How much of this snapshot the scorer read as digests rather than as text.

    A stage-1 failure is silent by design — the article still reaches the model,
    just in the pre-digest title+summary shape — so a bundle where a third of
    the digests failed scores fine and says nothing. That matters twice over:
    the fallback carries a truncated body instead of a structured digest, which
    is different evidence, and it carries it at several times the token cost.

    A probe run over twenty stored bundles had at least one failure in six of
    them, all of them the digest model running to its 16,384-token output limit
    on a prompt of one to four thousand. That is a third of the sample scoring
    on partly-degraded evidence, and the divergence meter is the deliverable.
    """
    items = [it for it in (items or []) if isinstance(it, dict)]
    degraded = [it for it in items if not isinstance(it.get("digest"), dict)]
    # A third state between "digested" and "degraded". The runaway retry sends
    # the first 6,000 characters, so what comes back is a digest of a truncated
    # article rather than of the article — better evidence than a truncated body,
    # and not the same thing as a clean digest. Recording it as clean would be a
    # recovery that silently changed what the evidence was.
    truncated = [it for it in items
                 if isinstance(it.get("digest"), dict)
                 and it["digest"].get("digest_source") == "truncated-retry"]
    return {
        "articles": len(items),
        "digested": len(items) - len(degraded),
        "degraded": len(degraded),
        "degraded_ids": sorted(str(it.get("id")) for it in degraded if it.get("id"))[:20],
        "truncated": len(truncated),
        "truncated_ids": sorted(str(it.get("id")) for it in truncated if it.get("id"))[:20],
    }


def _latest_year_of(series: Any) -> Optional[int]:
    """The newest year in one indicator's ``series`` that actually has a value."""
    if not isinstance(series, dict):
        return None
    years = []
    for year, value in series.items():
        if value is None:
            continue
        try:
            years.append(int(year))
        except (TypeError, ValueError):
            continue
    return max(years) if years else None


def macro_vintages(payload: Dict) -> Dict[str, Any]:
    """Vintage metadata for the macro panel this snapshot consumed.

    Full point-in-time macro is Phase B. What is knowable now is when the panel
    was generated and how recent each indicator's last observation is — enough
    to tell later whether a rating was built on a stale or a fresh panel.

    Args:
        payload: the dict from ``payload.prepare_llm_payload_pretty``.

    Returns:
        A dict that always carries ``vintage_scheme``; the rest degrades to None
        when the payload has no ``_meta`` (an older or hand-built payload).
    """
    payload = payload if isinstance(payload, dict) else {}
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    indicators = payload.get("indicators") if isinstance(payload.get("indicators"), dict) else {}

    return {
        "vintage_scheme": _VINTAGE_SCHEME,
        "panel_source": meta.get("source"),
        "panel_generated_at": meta.get("generated_at"),
        # Panel-wide newest year, as the payload reports it.
        "latest_year": payload.get("latest_year"),
        # Per indicator, because they lag by different amounts and an average of
        # 2025 CPI with 2021 governance is not the same evidence as all-2025.
        "latest_year_by_indicator": {
            name: _latest_year_of((data or {}).get("series"))
            for name, data in indicators.items() if isinstance(data, dict)
        },
    }


def build_input_manifest(*,
                         items: List[Dict],
                         prompt_entries: Optional[List[Dict]] = None,
                         fulltext_ids: Iterable[str] = (),
                         payload: Dict,
                         evidence: Optional[Dict] = None,
                         payload_health: Optional[Dict] = None,
                         model_id: Optional[str],
                         prompt_version: Optional[str],
                         policy_version: Optional[str],
                         seed: Optional[int],
                         masking: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything one snapshot needs to be reproducible.

    Args:
        items: the country's article dicts at LLM time.
        prompt_entries: ``langchain_llm.prompt_entries(items)``.
        fulltext_ids: ids whose full text the prompt carried.
        payload: the macro payload the prompt carried.
        payload_health: ``llm.payload.payload_health`` for this run — what the
            evidence payload actually carried against what the registry
            promised. Computed by the caller because `payload` imports this
            module and the resolution lives there.
        model_id: the scoring model, from the LLM result's own stamp rather than
            from the client — so the manifest records the model that answered.
        prompt_version: ``ai.constants.PROMPT_VERSION`` at call time.
        policy_version: ``ai.policy.POLICY_VERSION`` at call time.
        seed: the determinism seed (``ai.client.SEED``).
        masking: the regime this row was scored under —
            ``{scoring_mode, mask_map_version, mask_integrity_status,
            structural_fields, identifiability}``. Omitted for a named run.

            It belongs in the manifest rather than beside it because the
            manifest's promise is reproducibility, and under masking the bytes
            the model saw are not the bytes in the database: without the mask
            map's version the same articles re-mask differently and the row
            cannot be rebuilt. ``structural_fields`` counts the structural block
            because it is filled for five countries of forty-eight, and that
            asymmetry has to be visible in the data rather than only in a
            comment.

    Returns:
        The dict stored in ``risk_snapshot.input_manifest``. ``git_sha`` comes
        from the ``GIT_SHA`` environment variable and is None when unset — this
        never shells out to git, which would make the module impure and slow.
    """
    articles = build_article_manifest(items, prompt_entries, fulltext_ids)
    return {
        "schema_version": _SCHEMA_VERSION,
        "articles": articles,
        "stage1": stage1_health(items),
        "macro_vintages": macro_vintages(payload),
        "model_id": model_id,
        "prompt_version": prompt_version,
        "policy_version": policy_version,
        "payload_version": payload_version(),
        # The bytes the model actually reasoned over, which nothing hashed until
        # the trailing-context block made the omission expensive. `payload` above
        # is the *panel* payload — the DB-facing one — and only `macro_vintages`
        # reads it; the evidence payload reached the prompt and left no trace, so
        # `payload_version` recorded which contract was used and never which
        # evidence. A rebuild could differ in every number and match on every
        # recorded field.
        "evidence_sha256": text_sha256(
            _canonical_json(evidence) if evidence is not None else None),
        # The hash above proves two payloads differed; this says how. Computed
        # by `llm.payload.payload_health` -- it needs that module's own vintage
        # resolution, and `payload` already imports this one, so it cannot be
        # computed here. Passed in rather than derived, like everything else in
        # this module.
        **({"payload_health": payload_health} if payload_health else {}),
        # The corpus the anchor actually read, as one value. A three-arm
        # comparison is only about the payload if every arm selected the same
        # articles, and until now proving that meant reconstructing the
        # selection by hand from the article manifest.
        "article_set_sha256": text_sha256(_canonical_json(
            [e.get("url") for e in (articles or []) if isinstance(e, dict)])),
        "seed": seed,
        "git_sha": os.environ.get("GIT_SHA") or None,
        **({"masking": masking} if masking else {}),
    }
