"""Shared OpenAI client configuration for the three AI modules.

Single source for the model name and the deterministic-scoring settings
(temperature 0, fixed seed, no client-side retries — the callers degrade
gracefully on failure instead of retrying). Before this module existed the
model name lived in three call sites and one of them silently disagreed.
"""

from typing import Any, Optional

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


def build_chat(api_key: str) -> ChatOpenAI:
    """A ChatOpenAI configured for deterministic, non-retrying scoring calls."""
    return ChatOpenAI(
        model=MODEL_NAME,
        temperature=0.0,
        max_retries=0,
        api_key=api_key,
        seed=SEED,
    )


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
    """A ChatOpenAI for deterministic, non-retrying stage-1 calls."""
    return ChatOpenAI(
        model=DIGEST_MODEL_NAME,
        temperature=0.0,
        max_retries=0,
        api_key=api_key,
        seed=SEED,
        max_tokens=max_tokens,
    )


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
