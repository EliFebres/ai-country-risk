"""Shared OpenAI client configuration for the three AI modules.

Single source for the model name and the deterministic-scoring settings
(temperature 0, fixed seed, no client-side retries — the callers degrade
gracefully on failure instead of retrying). Before this module existed the
model name lived in three call sites and one of them silently disagreed.

Two models, and each is reachable at a different endpoint, because a bake-off
needs a candidate scorer and the incumbent stage-1 model alive in one process.
The environment is consulted per endpoint and every variable falls back to the
literal beside it, so an unset environment is byte-identical to the behaviour
before any of this existed. That is the property the daily run is owed: nobody
running `main.py` should be able to tell that a comparison harness exists.
"""

import json
import os
from typing import Any, Dict, Optional

from langchain_openai import ChatOpenAI

# The one scoring model. Pinned to a dated release so scores stay comparable
# run-to-run; bump deliberately, in one place.
MODEL_NAME = "gpt-4o-2024-08-06"

# The stage-1 digest model. Same pinning rationale: digests are cached by
# content hash, so an un-dated alias silently changing under us would make
# cached and fresh digests incomparable.
DIGEST_MODEL_NAME = "gpt-4o-mini-2024-07-18"

# The determinism seed both models run with. Named so the provenance manifest
# can stamp the value actually used instead of repeating the literal elsewhere;
# changing it makes new scores incomparable with every stored one.
SEED = 42


def scoring_model() -> str:
    """The scoring model actually in force, environment override included.

    Read rather than imported by anything that stamps a version, so a manifest
    and a freeze record what the call used and not what the file says.
    """
    return os.getenv("SCORING_MODEL") or MODEL_NAME


def digest_model() -> str:
    """The *article digest* model in force, environment override included.

    Deliberately narrower than it sounds. Four other callers build a stage-1
    client — the body rewrite, the digest sweep, the identifiability probe and
    the Wayback leakage scan — and every one of them is part of the masking
    instrument rather than of the digest. They stay on :data:`DIGEST_MODEL_NAME`
    whatever the environment says, so measuring a cheaper digest model cannot
    silently move the thing masking is judged on.
    """
    return os.getenv("DIGEST_MODEL") or DIGEST_MODEL_NAME


def _chat(model: str, api_key: str, *, prefix: str,
          max_tokens: Optional[int] = None) -> ChatOpenAI:
    """One ChatOpenAI, built the deterministic way, at ``prefix``'s endpoint.

    Args:
        prefix: which environment quad to consult — ``SCORING`` or ``DIGEST``.
            ``{prefix}_MODEL``, ``_BASE_URL``, ``_API_KEY`` and ``_EXTRA_BODY``
            all fall back to the OpenAI defaults, so an unset environment
            reaches OpenAI with the key the caller passed, exactly as before.

    ``_EXTRA_BODY`` is a JSON object of provider-specific request fields, and it
    lives in the environment rather than in a parameter because the call sites
    that would have to thread it — ``country_llm_score``, ``digest_articles`` —
    have no business knowing which vendor they are talking to. Its one use so
    far is DeepSeek's ``{"thinking": {"type": "disabled"}}``, where reasoning
    tokens bill as output: a pin that silently failed to apply would not error,
    it would just quietly cost several times the quoted price and report the
    wrong per-snapshot number. So malformed JSON raises here rather than being
    dropped.

    Raises:
        ValueError: ``{prefix}_EXTRA_BODY`` is set and is not a JSON object.
    """
    kwargs: Dict[str, Any] = {
        "model": model,
        "temperature": 0.0,
        "max_retries": 0,
        "api_key": os.getenv(prefix + "_API_KEY") or api_key,
        "seed": SEED,
    }
    base_url = os.getenv(prefix + "_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    extra_body = _extra_body(prefix)
    if extra_body:
        kwargs["extra_body"] = extra_body
    return ChatOpenAI(**kwargs)


def _extra_body(prefix: str) -> Optional[Dict[str, Any]]:
    """``{prefix}_EXTRA_BODY`` parsed, or None. Raises rather than ignoring."""
    raw = os.getenv(prefix + "_EXTRA_BODY")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{prefix}_EXTRA_BODY is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{prefix}_EXTRA_BODY must be a JSON object, "
                         f"got {type(parsed).__name__}")
    return parsed


def build_chat(api_key: str) -> ChatOpenAI:
    """A ChatOpenAI configured for deterministic, non-retrying scoring calls."""
    return _chat(scoring_model(), api_key, prefix="SCORING")


# What a digest can legitimately need. The schema is five short strings and a
# number; a real one lands near 200 tokens and has never been observed past 600.
#
# Uncapped, the stage-1 model does not merely exceed that — it runs to the
# 16,384-token output ceiling and dies there, on prompts of 2,794 to 5,393
# tokens. Seven times in one twenty-bundle probe run, every failure with
# `completion_tokens=16384` exactly. It is a generation loop, not a long answer,
# and the article ends up degraded either way; the cap decides only whether the
# loop costs $0.0098 or $0.0006.
#
# Over a 2,188-snapshot pilot at the observed failure rate that is the difference
# between roughly $10 of pure waste and roughly $0.65, against a $130 guard.
_DIGEST_MAX_TOKENS = 1024

# The same builder also serves the full-text mask rewrite, and that call is not
# a digest: its prompt says "changing nothing else, do not summarise", so its
# output is as long as its input. A cap sized for a five-field summary is a
# guillotine for a length-preserving rewrite — the median harvested body is
# ~5,300 characters, which cannot be reproduced inside 1,024 tokens at any
# ratio, so the call dies at the ceiling and the article degrades to
# title-only. Silently, on the two or three articles per snapshot the scorer
# weights most heavily.
#
# So the cap is a parameter rather than a constant, and the rewrite sizes it
# from what it is being asked to reproduce. The loop protection survives: a
# runaway on a long article stops at a multiple of that article's length
# instead of at the model's 16,384 ceiling.
_REWRITE_MAX_TOKENS_CEILING = 8192


def build_digest_chat(api_key: str,
                      max_tokens: int = _DIGEST_MAX_TOKENS) -> ChatOpenAI:
    """A ChatOpenAI for the stage-1 calls that are part of *masking*.

    Pinned to :data:`DIGEST_MODEL_NAME` and to OpenAI, with no environment
    override, because the rewrite, the sweep, the probe and the leakage scan
    are the instrument the masking claim rests on. Moving them is a change to
    what the pilot is measuring, not a change to what it costs.
    """
    return ChatOpenAI(model=DIGEST_MODEL_NAME, temperature=0.0, max_retries=0,
                      api_key=api_key, seed=SEED, max_tokens=max_tokens)


def build_stage1_chat(api_key: str,
                      max_tokens: int = _DIGEST_MAX_TOKENS) -> ChatOpenAI:
    """A ChatOpenAI for the article digest, which a bake-off may point elsewhere.

    The one stage-1 caller that follows ``DIGEST_MODEL`` / ``DIGEST_BASE_URL`` /
    ``DIGEST_API_KEY``. Separate from :func:`build_digest_chat` by name rather
    than by a flag, so a future caller has to decide which of the two it is
    instead of inheriting an override it never asked for.
    """
    return _chat(digest_model(), api_key, prefix="DIGEST",
                 max_tokens=max_tokens)


def rewrite_max_tokens(text: str) -> int:
    """Output budget for reproducing ``text`` with its proper nouns replaced.

    Three characters per token rather than the usual four, so the estimate errs
    long: running out mid-rewrite costs the article, and over-provisioning costs
    nothing — `max_tokens` is a ceiling, not a reservation, and the model is
    billed on what it emits.

    Floored at the digest cap so short articles behave exactly as before, and
    ceilinged below the model's own 16,384 so a generation loop still stops.
    Harvested bodies are truncated at 24,000 characters upstream, which is what
    the ceiling is sized for.
    """
    return min(_REWRITE_MAX_TOKENS_CEILING,
               max(_DIGEST_MAX_TOKENS, len(text) // 3))


def parse_importance(value: Any) -> Optional[float]:
    """Coerce a model-returned importance to a clamped 0..1 float, or None.

    The rankers must never crash on a malformed model value; callers skip the
    entry when this returns None.
    """
    try:
        importance = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, importance))
