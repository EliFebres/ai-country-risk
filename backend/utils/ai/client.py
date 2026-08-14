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


def build_digest_chat(api_key: str) -> ChatOpenAI:
    """A ChatOpenAI for deterministic, non-retrying stage-1 digest calls."""
    return ChatOpenAI(
        model=DIGEST_MODEL_NAME,
        temperature=0.0,
        max_retries=0,
        api_key=api_key,
        seed=SEED,
        max_tokens=_DIGEST_MAX_TOKENS,
    )


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
