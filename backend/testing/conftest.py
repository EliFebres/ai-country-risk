"""Pytest configuration for the backend suite.

The repo root is added to ``sys.path`` so ``backend.*`` imports resolve no
matter where pytest is invoked from. This mirrors what the entry point does at
startup; the suite must not depend on having been launched from one directory.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
