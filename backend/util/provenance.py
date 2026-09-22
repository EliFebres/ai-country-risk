"""
Which version of everything produced this.

Versions are derived, never remembered. A prompt version is the hash of the
prompt text, so it cannot drift from the prompt. The commit is read from git at
runtime, so it cannot be a string someone forgot to bump. Anything a human has
to remember to update is a version that will eventually be wrong while looking
right, which is worse than having none.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from typing import Any, Dict, Optional

from backend.util import paths

__all__ = ["git_sha", "run_versions"]


@lru_cache(maxsize=1)
def git_sha() -> Optional[str]:
    """Return the current commit, or None outside a working tree.

    Read rather than recorded. A deployment from a detached checkout with no git
    directory gets None, which is honest; a hard-coded constant would get a
    plausible wrong answer.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(paths.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (out.stdout or "").strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


def run_versions(
    *,
    scoring_model: str,
    digest_model: str,
    gate_model: str,
    prompt_version: str,
    digest_prompt_version: str,
    relevance_prompt_version: str,
    seed: int,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the version block stamped on every census row.

    Everything that could change an answer without the country changing belongs
    here. A score whose prompt and model are not recorded beside it cannot be
    compared with next week's.
    """
    block: Dict[str, Any] = {
        "git_sha": git_sha(),
        "scoring_model": scoring_model,
        "digest_model": digest_model,
        "gate_model": gate_model,
        "prompt_version": prompt_version,
        "digest_prompt_version": digest_prompt_version,
        "relevance_prompt_version": relevance_prompt_version,
        "seed": seed,
    }
    if extra:
        block.update(extra)
    return block
