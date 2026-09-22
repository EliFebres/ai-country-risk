"""The one executable.

    python -m backend.main etl              # the weekly ETL
    python -m backend.main prices           # the live-prices poll loop
    python -m backend.main prices --once    # a single tick, for verification
    python -m backend.main run              # the supervisor: both, one process

This file is argument parsing and dispatch. It holds no business logic: each
command is a few lines that hand off to a function in the folder that owns the
work. If a handler here grows past that, it is in the wrong file.

Handlers are named as strings and imported only when their command is chosen.
That is deliberate: --help, and the test that checks it, pull in argparse and
dotenv and nothing else - not pandas, duckdb, psycopg2 or langchain. It also
means a broken import in one command cannot stop another from running.

Before any command does anything it prints what it resolved - the command, the
project root, and the database it is pointed at, with the password stripped. Two
databases with confusable names cost a day once; naming the target on every run
line is the fix that stuck.
"""

import argparse
import pathlib
import sys
from importlib import import_module
from typing import Callable, Dict, NamedTuple

# --- Make "backend/" importable ----------------------------------------------
# Anchored on this file, never on the working directory. The package has no
# __init__.py and resolves as a PEP 420 namespace package, so the repo root must
# be on sys.path before the first backend.* import. This is the only place that
# duplicates what util/paths.py knows, because it has to run before util/paths.py
# can be imported; an invariant test asserts the two agree.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.util import env, paths  # noqa: E402  (must follow the sys.path insert)


class Command(NamedTuple):
    module: str
    function: str
    help: str


COMMANDS: Dict[str, Command] = {
    "etl": Command(
        "backend.util.pipeline", "run_etl",
        "run the weekly ETL once: panels, calendar, IMF, per-country scoring, alerts",
    ),
    "prices": Command(
        "backend.data_fetching.prices_daemon", "run_daemon",
        "poll live market prices continuously (--once for a single tick)",
    ),
    "run": Command(
        "backend.util.supervisor", "run_supervisor",
        "the supervisor: prices continuously, the ETL when the data says it is due",
    ),
}


def _resolve(command: str) -> Callable:
    """Import a handler only once its command has been chosen."""
    spec = COMMANDS[command]
    return getattr(import_module(spec.module), spec.function)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.main",
        description="AI Country Risk backend. One executable, three commands.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command", required=True)
    for name, spec in COMMANDS.items():
        sub = subparsers.add_parser(name, help=spec.help, description=spec.help)
        if name == "prices":
            sub.add_argument(
                "--once", action="store_true",
                help="run a single tick and exit, instead of looping",
            )
    return parser


def _announce(command: str) -> None:
    """Say what was resolved, before doing any work."""
    import os

    print(f"[main] command      : {command}")
    print(f"[main] project root : {paths.PROJECT_ROOT}")
    print(f"[main] database     : {env.redact_database_url(os.getenv('DATABASE_URL'))}")
    print(f"[main] env file     : {env.ENV_FILE} ({'found' if env.ENV_FILE.exists() else 'MISSING'})")
    print(f"[main] optional keys: {env.describe_optional()}")


def _utf8_console() -> None:
    """Make stdout UTF-8 regardless of the platform's default codepage.

    A Windows console defaults to cp1252, so a single non-ASCII character in a
    progress line — an arrow, an accented country name — raises
    UnicodeEncodeError and takes the whole run down with it. The run should not
    be able to fail on a print.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> None:
    _utf8_console()
    args = _build_parser().parse_args(argv)

    # Environment is loaded and validated once, here, before a handler is
    # imported - data_upsert snapshots DATABASE_URL at import time, so the
    # ordering is load, validate, then import.
    env.load()
    env.require(args.command)
    _announce(args.command)

    handler = _resolve(args.command)
    if args.command == "prices":
        handler(once=args.once)
    else:
        handler()


if __name__ == "__main__":
    main()
