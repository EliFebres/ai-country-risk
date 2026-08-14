"""Run the whole test suite.

    python backend/test.py             # everything
    python backend/test.py -k masking  # pytest args pass straight through

The one test executable, beside main.py the one runtime executable.

Opt into the Postgres-backed cases by setting ``HISTORY_TEST_DATABASE_URL``.
Without it the suite touches no network and no database, and spends nothing —
deliberately not ``DATABASE_URL``, so a bare run cannot create tables in
production.
"""

import pathlib
import subprocess
import sys

if __name__ == "__main__":
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.exit(subprocess.call(
        [sys.executable, "-m", "pytest", "backend/testing", *sys.argv[1:]],
        cwd=root))
