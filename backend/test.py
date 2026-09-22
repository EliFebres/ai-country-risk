"""Run the whole test suite.

    python backend/test.py             # everything
    python backend/test.py -k invariant # pytest args pass straight through

The one test executable, beside main.py the one runtime executable.

The suite touches no network, no database and no model. It spends nothing, and
it is meant to stay that way: a test that needs a live service belongs behind an
explicit opt-in variable, never behind ``DATABASE_URL``, so that a bare run can
never reach production.
"""

import pathlib
import subprocess
import sys

if __name__ == "__main__":
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.exit(subprocess.call(
        [sys.executable, "-m", "pytest", "backend/testing", *sys.argv[1:]],
        cwd=root))
