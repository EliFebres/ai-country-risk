"""Pytest configuration for the backend characterization suite.

These tests pin the *current* behavior of the backend's pure functions so the
refactor can prove it changed nothing. They touch no network and no database.

The repo root is added to ``sys.path`` so ``backend.*`` imports resolve no
matter where pytest is invoked from.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
