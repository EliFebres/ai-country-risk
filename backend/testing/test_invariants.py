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


def test_project_root_has_exactly_one_definition():
    """backend.util.paths is the only thing that decides where the project is.

    It imports nothing but pathlib, so this costs no dependency.
    """
    from backend.util import paths

    assert paths.PROJECT_ROOT == REPO_ROOT
    assert paths.BACKEND_DIR == BACKEND
    assert paths.DATA_DIR == BACKEND / "data"
    assert paths.PANEL_DIR == BACKEND / "data" / "wb_panel_wide"


def test_nothing_rewalks_the_filesystem_for_the_project_root():
    """No module may rediscover the root by walking up from the CWD.

    Three modules used to do this, two of them from the working directory, and
    the parquet panel's writer and reader could therefore disagree about where
    the panel was. The resolver in util/paths.py is anchored on its own file;
    a reintroduced walk would silently restore the divergence.
    """
    offenders = []
    for name, path in MODULES.items():
        if name == "backend.util.paths":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "Path.cwd()" in line or "os.getcwd()" in line:
                offenders.append(f"{name} (line {lineno}): {line.strip()}")
    assert not offenders, (
        "the project root is resolved in util/paths.py, not rediscovered:\n  "
        + "\n  ".join(offenders)
    )


def test_no_module_imports_backend_utils():
    """utils/ does not survive v2.0, under any spelling.

    This is also the guard against a compatibility shim: a re-export stub is a
    flag-off branch in disguise, and re-pointing one import back at backend.utils
    is how one would creep in.
    """
    offenders = []
    for name, path in MODULES.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                targets = [node.module]
            else:
                continue
            for t in targets:
                if t == "backend.utils" or t.startswith("backend.utils."):
                    offenders.append(f"{name} (line {node.lineno}): {t}")
    assert not offenders, "backend.utils is gone; still imported by:\n  " + "\n  ".join(offenders)


def test_utils_directory_is_gone():
    assert not (BACKEND / "utils").exists(), "backend/utils/ should not exist"


def test_every_module_lives_in_a_known_folder():
    """No new top-level folders, and nothing loose at the top of backend/.

    main.py and test.py are the two executables; every other module belongs to a
    folder that names what it is for.
    """
    allowed_folders = {
        "data_fetching", "news_fetching", "data_upsert",
        "llm", "notebooks", "util", "testing",
    }
    allowed_top_level = {"main.py", "test.py"}
    stray = []
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if len(rel.parts) == 1:
            if rel.name not in allowed_top_level:
                stray.append(f"{rel} (loose at the top of backend/)")
        elif rel.parts[0] not in allowed_folders:
            stray.append(f"{rel} (unknown folder {rel.parts[0]!r})")
    assert not stray, "unexpected module locations:\n  " + "\n  ".join(stray)


# --- the entry point ---------------------------------------------------------

EXPECTED_COMMANDS = {
    "etl": ("backend.util.pipeline", "run_etl"),
    "prices": ("backend.data_fetching.prices_daemon", "run_daemon"),
    "run": ("backend.util.supervisor", "run_supervisor"),
}


def test_main_declares_exactly_the_expected_commands():
    from backend import main

    assert set(main.COMMANDS) == set(EXPECTED_COMMANDS)


def test_each_command_dispatches_to_its_expected_function():
    """The table is only useful if the targets actually exist and are callable."""
    from backend import main

    for name, (module, function) in EXPECTED_COMMANDS.items():
        spec = main.COMMANDS[name]
        assert (spec.module, spec.function) == (module, function), name
        resolved = main._resolve(name)
        assert callable(resolved), f"{name} -> {module}.{function} is not callable"
        assert resolved.__name__ == function


def test_help_lists_exactly_the_commands():
    """--help is the contract a deployment reads. It must not drift."""
    import io
    import contextlib

    from backend import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            main._build_parser().parse_args(["--help"])
        except SystemExit:
            pass
    text = buf.getvalue()
    for name in EXPECTED_COMMANDS:
        assert f"\n    {name} " in text, f"{name} missing from --help:\n{text}"


def test_main_holds_no_business_logic():
    """main.py parses arguments and dispatches. Nothing else belongs in it.

    A crude proxy - the file's length - but the failure it guards against is
    gradual, and a number is harder to argue with than an intention.
    """
    lines = (BACKEND / "main.py").read_text(encoding="utf-8").splitlines()
    code = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    assert len(code) < 100, f"main.py has {len(code)} lines of code; it should only dispatch"


def test_entry_point_and_paths_agree_on_the_root():
    """main.py must bootstrap sys.path before util.paths can be imported, so the
    root is computed twice. The two must not be able to disagree."""
    from backend import main
    from backend.util import paths

    assert main._REPO_ROOT == paths.PROJECT_ROOT


# --- Consumer-side rules -----------------------------------------------------
#
# The recurring failure in this project has not been code that crashed. It has
# been code that ran, wrote something plausible, and had no consumer — so every
# count looked right and nobody ever read the number. These assert the reader
# exists, in the source, where a unit test with a stubbed writer cannot.


def test_a_scoring_run_writes_a_census():
    """The countermeasure only works if the run actually records one."""
    src = (BACKEND / "util" / "pipeline.py").read_text(encoding="utf-8")
    assert "payload_health.build_census(" in src, "the run builds no census"
    assert "store.write_census(" in src, "the run does not record a census"


def test_the_run_reads_the_census_back():
    """A write that returns cleanly is not evidence that anything landed, and a
    census nobody reads is the exact failure it exists to prevent."""
    src = (BACKEND / "util" / "pipeline.py").read_text(encoding="utf-8")
    assert "store.read_census(" in src, "the census is written and never read"


def test_the_resolution_alarm_has_a_caller():
    src = (BACKEND / "util" / "pipeline.py").read_text(encoding="utf-8")
    assert "resolution_alarm(" in src, "the alarm is computed by nobody"
    assert "read_recent_resolution(" in src, "the alarm has no baseline to compare against"


def test_the_relevance_gate_decides_what_is_scored():
    """A classifier whose verdict nothing acts on is the failure the gate
    replaced, wearing a different hat."""
    src = (BACKEND / "util" / "pipeline.py").read_text(encoding="utf-8")
    assert "relevance.classify(" in src
    assert "relevance.select(" in src


def test_the_keyword_relevance_heuristic_is_gone():
    """It returned 0.1 whenever the roster's formal name was not a substring of
    the title and summary, which floors every country the press calls by
    another name."""
    src = (BACKEND / "util" / "pipeline.py").read_text(encoding="utf-8")
    assert "_score_article_relevance" not in src


def test_nothing_stamps_an_indicator_as_of_from_the_clock():
    """`as_of` answers when a number became knowable, which is a fact about the
    publisher and never about when we happened to fetch it. Every date comes
    from `util.vintage`; this is the rule the fetch-clock bug broke."""
    offenders = []
    for name, path in MODULES.items():
        if name.endswith("util.vintage"):
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "as_of" not in stripped:
                continue
            if any(clock in stripped for clock in ("now()", "today()", "utcnow(")):
                offenders.append(f"{name} (line {i}): {stripped}")
    # Three exceptions, each named rather than waved through. All three are the
    # date we SCORED on, which is legitimately the clock; the rule is about when
    # a published number became knowable, which never is.
    allowed = (
        # the snapshot's own date, and the alerts feed's
        "run_as_of = datetime.now",
        "as_of=datetime.now",
        # the legal gate asks "which rules were in force when we scored", and
        # falls back to today when the payload carries no date. It becomes an
        # observation-only badge when the scorer is rewritten, which should
        # remove this line rather than justify it.
        "as_of = _parse_iso_date(as_of_raw)",
    )
    offenders = [o for o in offenders if not any(a in o for a in allowed)]
    assert not offenders, offenders


def test_the_prompt_version_is_derived_from_the_prompt():
    """A version somebody has to remember to bump is a version that will
    eventually be wrong while looking right."""
    for module, constant in (
        ("llm/constants.py", "PROMPT_VERSION"),
        ("llm/relevance.py", "RELEVANCE_PROMPT_VERSION"),
        ("llm/digest_engine.py", "DIGEST_PROMPT_VERSION"),
    ):
        src = (BACKEND / module).read_text(encoding="utf-8")
        assert f"{constant} = content_hash(" in src, f"{module}:{constant}"


def test_the_scoring_model_is_named_once_and_dated():
    """Four call sites used to carry this string and one had already drifted to
    the undated alias, so scores from two models were indistinguishable."""
    src = (BACKEND / "util" / "pipeline.py").read_text(encoding="utf-8")
    assert src.count('"gpt-4o-2024-08-06"') == 1, "the model id is repeated"
    for module in ("llm/relevance.py", "llm/digest_engine.py"):
        text = (BACKEND / module).read_text(encoding="utf-8")
        assert "DEFAULT_MODEL = \"gpt-4" in text
        # A dated id, not an alias: an alias moves under you.
        for line in text.splitlines():
            if line.startswith("DEFAULT_MODEL"):
                assert any(ch.isdigit() for ch in line.split("-")[-1]), line
