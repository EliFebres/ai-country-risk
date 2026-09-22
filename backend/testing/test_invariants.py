"""Cross-cutting rules about the shape of the backend package.

These are deliberately built on ``ast`` rather than on real imports: they need
no third-party package to run, they cover modules that would fail to import for
unrelated reasons, and they see a cycle that lazy importing would hide.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent


def _module_name(path: Path) -> str:
    """backend/util/constants.py -> backend.util.constants"""
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _source_files() -> dict:
    """Every module under backend/, excluding the suite itself."""
    return {
        _module_name(p): p
        for p in sorted(BACKEND.rglob("*.py"))
        if "testing" not in p.relative_to(BACKEND).parts
    }


MODULES = _source_files()


def _internal_imports(path: Path, known: set) -> set:
    """Names of backend.* modules this file imports, at any nesting depth."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("backend."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module or not node.module.startswith("backend"):
                continue
            for alias in node.names:
                # `from backend.util import constants` imports a module;
                # `from backend.util.constants import X` imports a name.
                candidate = f"{node.module}.{alias.name}"
                found.add(candidate if candidate in known else node.module)
    return {f for f in found if f in known}


def _graph() -> dict:
    known = set(MODULES)
    return {name: _internal_imports(path, known) for name, path in MODULES.items()}


def test_every_module_parses():
    """A syntax error anywhere fails here rather than halfway through a run."""
    assert MODULES, "no modules found under backend/"
    for name, path in MODULES.items():
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_import_graph_is_acyclic():
    """No import cycles, including ones a deferred import would mask.

    Reports the actual cycle path, because 'there is a cycle' is not actionable.
    """
    graph = _graph()
    visiting, done, cycles = set(), set(), []

    def walk(node, stack):
        if node in done:
            return
        if node in visiting:
            cycles.append(" -> ".join(stack[stack.index(node):] + [node]))
            return
        visiting.add(node)
        for target in sorted(graph.get(node, ())):
            walk(target, stack + [target])
        visiting.discard(node)
        done.add(node)

    for node in sorted(graph):
        walk(node, [node])

    assert not cycles, "import cycle(s):\n  " + "\n  ".join(sorted(set(cycles)))
