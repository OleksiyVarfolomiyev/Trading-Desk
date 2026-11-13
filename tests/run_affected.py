"""Run only the tests a change can affect.

Each test module depends on the repository files its tests actually run. Every run records that: each
selected test module runs in its own process (in parallel), with Python's execution monitor noting which
repository files executed, and the result updates ``tests/.test_map.json`` (not committed). The next run
selects from that map: a test module runs when a changed file is among the files it ran, or is the test
itself. For a test module the map does not cover yet, the dependencies are read from its imports instead
(every ``import`` statement, including the ones inside functions, followed transitively): safe but broad.

Files that are not Python map by rule:

* ``web/`` and ``desk/dashboard_static/`` (the page, its script and styles) -> ``test_web_build``;
* documentation, notebooks, deploy scripts and data -> no tests;
* dependency lists (``requirements*.txt``), ``tests/`` helpers and anything unrecognised -> every test.

    python tests/run_affected.py              # uncommitted changes (staged, unstaged, untracked) vs HEAD
    python tests/run_affected.py --base HEAD~3   # everything changed since a commit
    python tests/run_affected.py --list       # show the selection without running it
    python tests/run_affected.py --all        # the whole suite (also rebuilds the map)
    python tests/run_affected.py FILE ...     # as if these files changed

Run it with the desk's test interpreter (py314; the monitor needs Python 3.12+, older versions run untraced).
A test that reaches code only through a string or a subprocess is covered by the rules above or by ``--all``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
MAP_FILE = TESTS / ".test_map.json"
# Where the tests put modules on sys.path (they import ``hl_daemon``, ``portfolio_analytics_implementation`` ...).
SEARCH = [ROOT, ROOT / "execution", ROOT / "research", ROOT / "web"]

# Non-Python files the tests read, by path prefix -> test modules.
FILE_RULES = {
    "web/": {"test_web_build"},
    "desk/dashboard_static/": {"test_web_build"},
}
# Changes no test can see.
NO_TESTS = ("deploy/", "research/data/", "data/", "docs/", ".claude/", ".github/")
NO_TEST_SUFFIXES = (".md", ".ipynb", ".csv", ".png", ".svg", ".gitignore")
# Changes that can affect any test.
EVERYTHING = ("requirements", "tests/", "setup.cfg", "pyproject.toml")

# Runs one test module and records the repository files that executed (sys.monitoring, Python 3.12+).
TRACED_RUN = r"""
import json, sys, unittest
from pathlib import Path
root, module, out = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, str(root))
seen = set()
monitor = getattr(sys, "monitoring", None)
if monitor is not None:
    tool = monitor.COVERAGE_ID
    monitor.use_tool_id(tool, "run_affected")
    def started(code, offset):
        seen.add(code.co_filename)
        return monitor.DISABLE  # one note per function is enough
    monitor.register_callback(tool, monitor.events.PY_START, started)
    monitor.set_events(tool, monitor.events.PY_START)
result = unittest.main(module=module, argv=["run_affected"], exit=False, verbosity=1).result
if monitor is not None:
    monitor.set_events(tool, 0)
    monitor.free_tool_id(tool)
files = set()
for name in list(seen):
    try:
        path = Path(name).resolve().relative_to(root)
    except ValueError:
        continue
    if path.parts and path.parts[0] not in (".venv", "venv") and path.suffix == ".py":
        files.add(str(path))
json.dump({"files": sorted(files), "traced": monitor is not None, "ok": result.wasSuccessful()}, open(out, "w"))
"""


# ------------------------------------------------------------------ static imports (fallback)
def resolve(name: str) -> Path | None:
    """The repository file for a dotted module name, or None for the standard library and packages."""
    parts = name.split(".")
    for base in SEARCH:
        for candidate in (base.joinpath(*parts).with_suffix(".py"), base.joinpath(*parts, "__init__.py")):
            if candidate.is_file():
                return candidate
    return None


@lru_cache(maxsize=None)
def imports(path: Path) -> frozenset[Path]:
    """The repository files ``path`` imports directly (packages bring their ``__init__``)."""
    try:
        tree = ast.parse(path.read_text(), str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return frozenset()
    found: set[Path] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: from . import x / from .x import y
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                prefix = ".".join(base.relative_to(ROOT).parts)
                module = f"{prefix}.{node.module}" if node.module else prefix
            else:
                module = node.module or ""
            names = [module] + [f"{module}.{alias.name}" for alias in node.names]
        for name in names:
            parts = name.split(".")
            for end in range(1, len(parts) + 1):  # desk.hl.x also loads desk/__init__ and desk/hl/__init__
                target = resolve(".".join(parts[:end]))
                if target is not None and target != path:
                    found.add(target)
    return frozenset(found)


def static_closure(path: Path) -> set[str]:
    """Every repository file ``path`` may import, itself included, as paths relative to the repository."""
    seen, stack = {path}, [path]
    while stack:
        for target in imports(stack.pop()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return {str(item.relative_to(ROOT)) for item in seen}


# ------------------------------------------------------------------ selection
def test_modules() -> list[str]:
    return sorted(path.stem for path in TESTS.glob("test_*.py"))


def load_map() -> dict[str, list[str]]:
    try:
        return json.loads(MAP_FILE.read_text())
    except (OSError, ValueError):
        return {}


def dependencies(test: str, recorded: dict[str, list[str]]) -> tuple[set[str], str]:
    """The files ``test`` depends on, and where that came from ("recorded" or "imports")."""
    own = f"tests/{test}.py"
    if test in recorded:
        return set(recorded[test]) | {own}, "recorded"
    return static_closure(TESTS / f"{test}.py"), "imports"


def changed_files(base: str | None) -> list[str]:
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]

    files = set(git("diff", "--name-only", base or "HEAD"))
    if base is None:
        files |= set(git("ls-files", "--others", "--exclude-standard"))
    return sorted(files)


def select(changed: list[str], recorded: dict[str, list[str]]) -> tuple[set[str], dict[str, str]]:
    """The test modules to run and, for each changed file, why."""
    every = set(test_modules())
    deps = {test: dependencies(test, recorded)[0] for test in every}
    chosen: set[str] = set()
    reasons: dict[str, str] = {}
    for name in changed:
        is_test = bool(re.fullmatch(r"tests/test_\w+\.py", name))
        if is_test:
            stem = Path(name).stem
            if stem in every:
                chosen.add(stem)
            reasons[name] = "the test itself"
        elif name.startswith(EVERYTHING):
            chosen |= every
            reasons[name] = "can affect any test: everything"
        elif (rule := next((tests for prefix, tests in FILE_RULES.items() if name.startswith(prefix)), None)) \
                and not name.endswith(".py"):
            chosen |= rule
            reasons[name] = ", ".join(sorted(rule))
        elif name.startswith(NO_TESTS) or name.endswith(NO_TEST_SUFFIXES):
            reasons[name] = "no tests (docs, data, notebooks or deploy)"
        elif name.endswith(".py"):
            hits = {test for test, files in deps.items() if name in files}
            chosen |= hits
            reasons[name] = ", ".join(sorted(hits)) if hits else "no test runs it"
        else:
            chosen |= every
            reasons[name] = "unrecognised file type: everything"
    return chosen, reasons


# ------------------------------------------------------------------ running
def run_one(test: str, scratch: Path) -> tuple[str, dict, str]:
    out = scratch / f"{test}.json"
    process = subprocess.run([sys.executable, "-c", TRACED_RUN, str(ROOT), f"tests.{test}", str(out)],
                             cwd=ROOT, capture_output=True, text=True)
    try:
        report = json.loads(out.read_text())
    except (OSError, ValueError):  # the module could not even load
        report = {"files": [], "traced": False, "ok": False}
    report["ok"] = report["ok"] and process.returncode == 0
    return test, report, process.stderr


def run(tests: list[str]) -> int:
    import tempfile

    recorded = load_map()
    total_ran, failed = 0, []
    with tempfile.TemporaryDirectory() as folder, ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        for test, report, output in pool.map(lambda name: run_one(name, Path(folder)), tests):
            ran = re.search(r"^Ran (\d+) tests?", output, re.M)
            total_ran += int(ran.group(1)) if ran else 0
            if report["traced"] and report["ok"]:
                recorded[test] = report["files"]
            if not report["ok"]:
                failed.append(test)
                print(f"\n===== {test} FAILED =====\n{output.strip()}\n")
    recorded = {test: files for test, files in recorded.items() if test in set(test_modules())}
    MAP_FILE.write_text(json.dumps(recorded, indent=1, sort_keys=True))
    print(f"Ran {total_ran} tests in {len(tests)} modules: "
          + (f"FAILED ({', '.join(failed)})" if failed else "OK"))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", help="compare against this commit instead of the uncommitted changes")
    parser.add_argument("--list", action="store_true", help="show the selection without running it")
    parser.add_argument("--all", action="store_true", help="run the whole suite and rebuild the map")
    parser.add_argument("files", nargs="*", help="treat these files as the change instead of git's list")
    args = parser.parse_args(argv)

    every = test_modules()
    if args.all:
        chosen = set(every)
    else:
        recorded = load_map()
        chosen, reasons = select(args.files or changed_files(args.base), recorded)
        for name, why in reasons.items():
            print(f"  {name}: {why}")
        unmapped = [test for test in chosen if test not in recorded]
        if unmapped:
            print(f"  (no recorded map yet for {len(unmapped)} of them: selected from imports)")
    print(f"{len(chosen)} of {len(every)} test modules selected"
          + (f": {', '.join(sorted(chosen))}" if chosen and len(chosen) < len(every) else "."))
    if args.list or not chosen:
        return 0
    return run(sorted(chosen))


if __name__ == "__main__":
    raise SystemExit(main())
