"""Import every backend module and report cycles. Run after each move commit.

`pkgutil.walk_packages` does not recurse into namespace packages, and this tree
has no ``__init__.py`` anywhere, so the module list comes off the filesystem.
"""

import importlib
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

SKIP = {"__pycache__", "tests", "testing", "notebooks", ".venv"}


def modules():
    """Dotted names for every backend .py file outside the skip list."""
    for path in sorted((ROOT / "backend").rglob("*.py")):
        rel = path.relative_to(ROOT).with_suffix("")
        if SKIP & set(rel.parts):
            continue
        yield ".".join(rel.parts)


def main() -> int:
    failed = []
    names = list(modules())
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report every failure, not the first
            kind = "CYCLE" if isinstance(exc, ImportError) and "circular" in str(exc) \
                else type(exc).__name__
            failed.append((name, kind, exc))

    for name, kind, exc in failed:
        print(f"FAIL {name}: {kind}: {exc}")
        if kind == "CYCLE":
            traceback.print_exception(type(exc), exc, exc.__traceback__)

    print(f"{len(names) - len(failed)}/{len(names)} modules import cleanly")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
