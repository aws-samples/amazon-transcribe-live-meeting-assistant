#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Measure line coverage for every test suite in the repository and print a table.

Run via `make test-coverage`. This **reports**; it does not enforce. Each
component has a recorded floor in `scripts/coverage_floors.json` and the table
says whether it is met, but the exit code still reflects only whether the suites
themselves ran. `--enforce` makes a breach exit non-zero and no make target
passes it — adding it to one is the single change that turns these numbers into a
gate, once they are trusted enough to gate on.

The floors exist so a regression is *visible* rather than punished: each is the
measured figure rounded down to a whole percent, so they catch coverage going
backwards without demanding that anyone improve it first.

Two things it takes care to get right, because a coverage number that flatters
the codebase is worse than none at all:

* **Files no test ever imports are counted.** A coverage tool measures what runs,
  so a module that nothing imports is absent from its report rather than being
  reported at zero — which silently removes the least-covered code from the
  denominator. For the Python suites every source file on disk is accounted for,
  with unimported ones parsed for their statement count and credited nothing.
* **Test files are excluded** from the measurement, so a suite cannot raise its
  own number by being large.

The Node suites are measured by c8 (`node --test`, source-mapped back to the
TypeScript) and by vitest's v8 provider (the UI). Those report only files that
were loaded, so for them the unimported files are counted and shown separately
rather than folded into the percentage; the column is labelled accordingly.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import coverage

REPO = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO / ".venv" / "bin" / "python"
ASR_VENV_PYTHON = REPO / "lma-asr-microvm-stack" / "source" / ".venv" / "bin" / "python"

# Written to a temporary directory; nothing is left in the working tree.
_WORK = Path(tempfile.mkdtemp(prefix="lma-coverage-"))


@dataclass
class Result:  # pylint: disable=too-many-instance-attributes
    """One component's measurement, or the reason there isn't one."""

    name: str
    language: str
    covered: int = 0
    total: int = 0
    files_measured: int = 0
    files_unimported: int = 0
    unimported_counted_in_total: bool = False
    failed: str | None = None

    @property
    def percent(self) -> float:
        """Line coverage, or 0.0 for a component with nothing to measure."""
        return 100.0 * self.covered / self.total if self.total else 0.0


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a command, returning its exit code and combined output."""
    merged = {**os.environ, **(env or {})}
    # Every suite here mocks AWS, but a suite that regressed into a real call
    # should fail rather than pick up the developer's credentials.
    merged.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    merged.setdefault("AWS_REGION", "us-east-1")
    proc = subprocess.run(
        cmd, cwd=cwd, env=merged, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout + proc.stderr


# ── Python ──────────────────────────────────────────────────────────────────


def _python_sources(roots: list[Path]) -> set[Path]:
    """Every non-test Python source file under `roots`."""
    found: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if parts & {".venv", "node_modules", "__pycache__", ".aws-sam", "build"}:
                continue
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                continue
            found.add(path.resolve())
    return found


def _statements_in(path: Path) -> int:
    """Statement count for a file no test imported, so it can be scored zero.

    Uses coverage's own parser, so the number is on the same footing as the ones
    in its JSON report rather than a line count that would not compare.
    """
    try:
        cov = coverage.Coverage(data_file=None)
        _, statements, _, _, _ = cov.analysis2(str(path))
        return len(statements)
    # Any parse failure (syntax the running interpreter rejects, an unreadable
    # file) scores nothing rather than stopping the report.
    except Exception:  # pylint: disable=broad-exception-caught
        return 0


def _summarize_python(report: Path, roots: list[Path], result: Result) -> None:
    """Fold a coverage JSON report plus the unimported files into `result`.

    Refuses to report a number if `roots` matches nothing on disk. A mistyped
    root would otherwise look like a component whose every file is covered,
    which is the one way this script could quietly flatter the codebase.
    """
    sources = _python_sources(roots)
    if not sources:
        result.failed = (
            "source roots matched no .py files: "
            f"{[str(r.relative_to(REPO)) for r in roots]}"
        )
        return

    data = json.loads(report.read_text())
    measured: set[Path] = set()
    for name, entry in data["files"].items():
        measured.add((REPO / name).resolve() if not Path(name).is_absolute() else Path(name))
        summary = entry["summary"]
        result.covered += summary["covered_lines"]
        result.total += summary["num_statements"]
    result.files_measured = len(data["files"])

    unimported = sources - measured
    result.files_unimported = len(unimported)
    result.unimported_counted_in_total = True
    for path in unimported:
        result.total += _statements_in(path)


def measure_python_package(name: str, cwd: Path, cov_target: str, roots: list[Path]) -> Result:
    """A suite that lives beside an installed package (the SDK and the CLI)."""
    result = Result(name=name, language="python")
    slug = name.replace(" ", "-").lower()
    report = _WORK / f"{slug}.json"
    code, output = _run(
        [
            str(VENV_PYTHON), "-m", "pytest", "tests/", "-q",
            f"--cov={cov_target}", f"--cov-report=json:{report}", "--cov-report=",
        ],
        cwd=cwd,
        # Keeps coverage's own data file out of the working tree, which would
        # otherwise gain a stray .coverage per component.
        env={"COVERAGE_FILE": str(_WORK / f".coverage-{slug}")},
    )
    if code != 0 or not report.exists():
        result.failed = output.strip().splitlines()[-1] if output.strip() else "suite failed"
        return result
    _summarize_python(report, roots, result)
    return result


def measure_lambda_functions() -> Result:
    """The Lambda suites, which run one directory at a time.

    Each function's tests import their module as a sibling, so a single pytest
    over the tree collides on duplicate module names — the same reason
    `make test-lambdas` loops. Coverage is accumulated into one data file across
    those runs, then reported once.
    """
    result = Result(name="Lambda functions", language="python")
    root = REPO / "lma-ai-stack" / "source" / "lambda_functions"
    data_file = _WORK / ".coverage-lambdas"
    rcfile = _WORK / "lambdas.coveragerc"
    rcfile.write_text(
        "[run]\n"
        "branch = True\n"
        "omit =\n"
        "    */test_*.py\n"
        "    */__pycache__/*\n"
    )

    test_dirs = sorted({path.parent for path in root.rglob("test_*.py")})
    if not test_dirs:
        result.failed = "no Lambda test directories found"
        return result

    for directory in test_dirs:
        code, output = _run(
            [
                str(VENV_PYTHON), "-m", "pytest", "-q",
                *(p.name for p in sorted(directory.glob("test_*.py"))),
                f"--cov={directory}", "--cov-append", "--cov-report=",
                f"--cov-config={rcfile}",
            ],
            cwd=directory,
            env={"COVERAGE_FILE": str(data_file)},
        )
        if code != 0:
            result.failed = f"{directory.name}: {output.strip().splitlines()[-1]}"
            return result

    report = _WORK / "lambdas.json"
    code, output = _run(
        [str(VENV_PYTHON), "-m", "coverage", "json", f"--rcfile={rcfile}",
         "-o", str(report), "--quiet"],
        cwd=REPO,
        env={"COVERAGE_FILE": str(data_file)},
    )
    if code != 0 or not report.exists():
        result.failed = f"coverage json failed: {output.strip()[-200:]}"
        return result
    _summarize_python(report, [root], result)
    return result


def measure_lambda_layers() -> Result:
    """The shared Lambda layer: the code every Lambda imports.

    Measured rather than merely parsed, so the figure moves when tests are added.
    Its suites live beside the packages they cover and rely on the layer root
    being importable (`from sentiment import ...`), so pytest is run from that
    root rather than from each test's own directory.

    A layer with no tests at all still gets a row: every file is then counted at
    zero, so the gap shows as a number instead of as an absence.
    """
    result = Result(name="Lambda layers", language="python")
    root = REPO / "lma-ai-stack" / "source" / "lambda_layers"
    layer_roots = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    if not layer_roots:
        result.failed = f"no layer directories under {root}"
        return result

    rcfile = _WORK / "layers.coveragerc"
    rcfile.write_text(
        "[run]\n"
        "branch = True\n"
        "omit =\n"
        "    */test_*.py\n"
        "    */__pycache__/*\n"
    )
    data_file = _WORK / ".coverage-layers"
    report = _WORK / "layers.json"

    ran_any = False
    for layer in layer_roots:
        if not list(layer.rglob("test_*.py")):
            continue
        ran_any = True
        code, output = _run(
            [
                str(VENV_PYTHON), "-m", "pytest", "-q", ".",
                f"--cov={layer}", "--cov-append", "--cov-report=",
                f"--cov-config={rcfile}",
            ],
            cwd=layer,
            env={"COVERAGE_FILE": str(data_file)},
        )
        if code != 0:
            result.failed = f"{layer.name}: {output.strip().splitlines()[-1]}"
            return result

    if not ran_any:
        # No suite anywhere in the layer: score every file zero rather than
        # leaving the component out of the table.
        sources = _python_sources([root])
        result.files_unimported = len(sources)
        result.unimported_counted_in_total = True
        result.total = sum(_statements_in(path) for path in sources)
        return result

    code, output = _run(
        [str(VENV_PYTHON), "-m", "coverage", "json", f"--rcfile={rcfile}",
         "-o", str(report), "--quiet"],
        cwd=REPO,
        env={"COVERAGE_FILE": str(data_file)},
    )
    if code != 0 or not report.exists():
        result.failed = f"coverage json failed: {output.strip()[-200:]}"
        return result
    _summarize_python(report, [root], result)
    return result


def measure_asr() -> Result:
    """The ASR MicroVM runtime, which uses its own virtualenv."""
    result = Result(name="ASR MicroVM", language="python")
    source = REPO / "lma-asr-microvm-stack" / "source"
    if not ASR_VENV_PYTHON.exists():
        result.failed = "no .venv — run `make test-asr` once to create it"
        return result
    report = _WORK / "asr.json"
    packages = ["asr_microvm", "asr_protocol", "asr_server"]
    code, output = _run(
        [
            str(ASR_VENV_PYTHON), "-m", "pytest", "-q",
            *(f"--cov={pkg}" for pkg in packages),
            f"--cov-report=json:{report}", "--cov-report=",
        ],
        cwd=source,
        env={"COVERAGE_FILE": str(_WORK / ".coverage-asr")},
    )
    if code != 0 or not report.exists():
        hint = output.strip().splitlines()[-1] if output.strip() else "suite failed"
        if "unrecognized arguments" in output or "No module named" in output:
            hint = "pytest-cov missing — run `make test-asr` to refresh its venv"
        result.failed = hint
        return result
    _summarize_python(report, [source / pkg for pkg in packages], result)
    return result


# ── Node ────────────────────────────────────────────────────────────────────


@dataclass
class NodeSuite:  # pylint: disable=too-many-instance-attributes
    """A JavaScript/TypeScript suite and how to get a coverage summary from it."""

    name: str
    cwd: Path
    command: list[str]
    source_root: Path
    extensions: tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx")
    ignore_dirs: tuple[str, ...] = ("node_modules", "dist", "build", "coverage")
    report_subpath: str = "coverage-summary.json"
    extra_env: dict[str, str] = field(default_factory=dict)


def _node_sources(suite: NodeSuite) -> set[Path]:
    """Every non-test source file under the suite's source root."""
    found: set[Path] = set()
    if not suite.source_root.exists():
        return found
    for path in suite.source_root.rglob("*"):
        if not path.is_file() or path.suffix not in suite.extensions:
            continue
        if set(path.parts) & set(suite.ignore_dirs):
            continue
        if ".test." in path.name or ".spec." in path.name:
            continue
        found.add(path.resolve())
    return found


def measure_node(suite: NodeSuite) -> Result:
    """Run a Node suite under its coverage tool and read the summary it writes."""
    result = Result(name=suite.name, language="node")
    if not (suite.cwd / "node_modules").exists():
        result.failed = "node_modules missing — run `npm ci` in this component"
        return result
    report_dir = _WORK / suite.name.replace(" ", "-").lower()
    command = [part.replace("@REPORT_DIR@", str(report_dir)) for part in suite.command]
    code, output = _run(command, cwd=suite.cwd, env=suite.extra_env)
    report = report_dir / suite.report_subpath
    if not report.exists():
        tail = output.strip().splitlines()[-1] if output.strip() else "no report produced"
        result.failed = f"exit {code}: {tail}"
        return result

    data = json.loads(report.read_text())
    totals = data.pop("total", {}).get("lines", {})
    result.covered = totals.get("covered", 0)
    result.total = totals.get("total", 0)
    result.files_measured = len(data)
    measured = {Path(name).resolve() for name in data if Path(name).is_absolute()}
    # Percentages are over loaded files only; see the module docstring.
    result.files_unimported = len(_node_sources(suite) - measured)
    result.unimported_counted_in_total = False
    return result


def node_suites() -> list[NodeSuite]:
    """The three JavaScript/TypeScript suites, in the order they are reported."""
    vp = REPO / "lma-virtual-participant-stack" / "backend"
    ws = REPO / "lma-websocket-transcriber-stack" / "source" / "app"
    ui = REPO / "lma-ai-stack" / "source" / "ui"
    return [
        NodeSuite(
            name="VP backend",
            cwd=vp,
            # c8 wraps the same `node --test dist/*.test.js` the suite normally
            # runs; the inline source maps put the result back on src/*.ts.
            command=[
                "npx", "c8", "--reporter=json-summary",
                "--report-dir=@REPORT_DIR@",
                # c8 defaults its scratch to ./coverage/tmp, which would
                # leave a directory behind in the component.
                "--temp-directory=@REPORT_DIR@/tmp",
                "--exclude", "**/*.test.*",
                "node", "--test", "dist/*.test.js",
            ],
            source_root=vp / "src",
        ),
        NodeSuite(
            name="WebSocket transcriber",
            cwd=ws,
            command=[
                "npx", "c8", "--reporter=json-summary",
                "--report-dir=@REPORT_DIR@",
                # c8 defaults its scratch to ./coverage/tmp, which would
                # leave a directory behind in the component.
                "--temp-directory=@REPORT_DIR@/tmp",
                "--exclude", "**/*.test.*",
                "node", "--test", "dist/**/*.test.js",
            ],
            source_root=ws / "src",
        ),
        NodeSuite(
            name="React UI",
            cwd=ui,
            command=[
                "npx", "vitest", "run", "--coverage",
                "--coverage.reporter=json-summary",
                "--coverage.reportsDirectory=@REPORT_DIR@",
            ],
            source_root=ui / "src",
            extra_env={"CI": "true"},
        ),
    ]


def build_node_first(suite: NodeSuite) -> str | None:
    """`node --test` runs compiled output, so it has to be current."""
    if suite.name == "React UI":
        return None
    code, output = _run(["npm", "run", "build"], cwd=suite.cwd)
    if code != 0:
        return f"build failed: {output.strip().splitlines()[-1]}"
    return None


# ── output ──────────────────────────────────────────────────────────────────


FLOORS_FILE = REPO / "scripts" / "coverage_floors.json"

# How far above its floor a component has to climb before the report suggests
# raising it. Below this the slack is treated as ordinary measurement jitter.
_RAISE_SUGGESTION_MARGIN = 2.0


def load_floors() -> dict[str, int]:
    """Per-component floors, or an empty mapping if the file is absent."""
    if not FLOORS_FILE.exists():
        return {}
    return json.loads(FLOORS_FILE.read_text()).get("floors", {})


def verdict_for(item: Result, floors: dict[str, int]) -> tuple[str, str]:
    """Return (label, note) comparing a component against its floor."""
    if item.failed:
        return "n/a", ""
    if item.name not in floors:
        return "—", f"{item.name}: no floor recorded in {FLOORS_FILE.name}"
    floor = floors[item.name]
    if item.percent < floor:
        return "BELOW", (
            f"{item.name}: {item.percent:.1f}% is below its floor of {floor}% — "
            f"coverage has regressed since the floor was set"
        )
    if item.percent >= floor + _RAISE_SUGGESTION_MARGIN:
        return "OK", (
            f"{item.name}: {item.percent:.1f}% is well above its floor of {floor}% — "
            f"raise the floor in {FLOORS_FILE.name} to hold the gain"
        )
    return "OK", ""


TABLE_HEADERS = (
    "Component", "Lang", "Lines", "Covered", "Total", "Files", "No data",
    "Floor", "",
)


def _build_rows(
    results: list[Result], floors: dict[str, int]
) -> tuple[list[tuple[str, ...]], list[str]]:
    """One display row per component, plus any notes the verdicts produced."""
    rows: list[tuple[str, ...]] = []
    notes: list[str] = []
    for item in results:
        label, note = verdict_for(item, floors)
        if note:
            notes.append(note)
        floor_text = f"{floors[item.name]}%" if item.name in floors else "—"
        if item.failed:
            rows.append((item.name, item.language, "n/a", "—", "—", "—", "—",
                         floor_text, label))
            continue
        rows.append((
            item.name,
            item.language,
            f"{item.percent:.1f}%",
            f"{item.covered}",
            f"{item.total}",
            f"{item.files_measured}",
            f"{item.files_unimported}{'' if item.unimported_counted_in_total else '*'}",
            floor_text,
            label,
        ))
    return rows, notes


def print_table(results: list[Result], floors: dict[str, int] | None = None) -> None:
    """Print the per-component table, the two language totals, and the caveats."""
    floors = floors if floors is not None else {}
    headers = TABLE_HEADERS
    rows, notes = _build_rows(results, floors)

    # The two footer labels are wider than some component names, so they have to
    # be in the width calculation or the separator rules come out ragged.
    footer_labels = [("Python (all)",), ("Node (all)",)]
    widths = [
        max(len(str(row[i])) for row in (headers, *rows, *footer_labels) if i < len(row))
        for i in range(len(headers))
    ]

    def line(cells) -> str:
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()

    print()
    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(line(row))

    measured = [r for r in results if not r.failed]
    python_total = sum(r.total for r in measured if r.language == "python")
    python_covered = sum(r.covered for r in measured if r.language == "python")
    node_total = sum(r.total for r in measured if r.language == "node")
    node_covered = sum(r.covered for r in measured if r.language == "node")
    print("  ".join("-" * w for w in widths))
    if python_total:
        print(line((
            "Python (all)", "python", f"{100 * python_covered / python_total:.1f}%",
            python_covered, python_total, "", "", "", "",
        )))
    if node_total:
        print(line((
            "Node (all)", "node", f"{100 * node_covered / node_total:.1f}%",
            node_covered, node_total, "", "", "", "",
        )))

    _print_footer(results, floors, notes)


def _print_footer(results: list[Result], floors: dict[str, int], notes: list[str]) -> None:
    """The column key, the per-component notes, and the advisory verdict."""
    print()
    print("Lines  = line (statement) coverage. Files = files the suite loaded.")
    print("No data = source files no test imported. These are counted as zero in")
    print("          the Python totals; for Node (*) they are excluded from the")
    print("          percentage, which is therefore optimistic.")
    print(f"Floor  = the recorded floor from {FLOORS_FILE.name}.")

    if notes:
        print()
        print("Notes:")
        for note in notes:
            print(f"  {note}")

    below = [r for r in results if verdict_for(r, floors)[0] == "BELOW"]
    print()
    if below:
        print(f"{len(below)} component(s) are below their recorded floor. This is")
        print("ADVISORY: the run still succeeds. Pass --enforce to make a breach")
        print("exit non-zero (no make target does yet).")
    else:
        print("Every measured component is at or above its recorded floor. Floors")
        print("are advisory — nothing here can fail a build without --enforce.")

    failures = [r for r in results if r.failed]
    if failures:
        print()
        print("Suites that did not produce a measurement:")
        for item in failures:
            print(f"  {item.name}: {item.failed}")


def main(argv: list[str] | None = None) -> int:
    """Measure the selected components and print the table; returns an exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="NAME",
        help="Measure only components whose name contains NAME (repeatable).",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="Also write the measurements to PATH as JSON.",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help=(
            "Exit non-zero when a component is below its floor in "
            "scripts/coverage_floors.json. Off by default: the floors are "
            "advisory, and no make target passes this yet."
        ),
    )
    args = parser.parse_args(argv)
    floors = load_floors()

    def wanted(name: str) -> bool:
        return not args.only or any(f.lower() in name.lower() for f in args.only)

    results: list[Result] = []
    try:
        if wanted("LMA SDK"):
            results.append(measure_python_package(
                "LMA SDK", REPO / "lib" / "lma_sdk", "lma_sdk",
                [REPO / "lib" / "lma_sdk" / "lma_sdk"],
            ))
        if wanted("LMA CLI"):
            results.append(measure_python_package(
                "LMA CLI", REPO / "lib" / "lma_cli_pkg", "lma_cli",
                [REPO / "lib" / "lma_cli_pkg" / "lma_cli"],
            ))
        if wanted("Lambda functions"):
            results.append(measure_lambda_functions())
        if wanted("Lambda layers"):
            results.append(measure_lambda_layers())
        if wanted("ASR MicroVM"):
            results.append(measure_asr())
        for suite in node_suites():
            if not wanted(suite.name):
                continue
            problem = build_node_first(suite)
            if problem:
                results.append(Result(name=suite.name, language="node", failed=problem))
                continue
            results.append(measure_node(suite))

        print_table(results, floors)

        if args.json:
            Path(args.json).write_text(json.dumps(
                [
                    {
                        "component": r.name,
                        "language": r.language,
                        "lines_covered": r.covered,
                        "lines_total": r.total,
                        "percent": round(r.percent, 2),
                        "files_measured": r.files_measured,
                        "files_without_data": r.files_unimported,
                        "unimported_in_total": r.unimported_counted_in_total,
                        "floor": floors.get(r.name),
                        "verdict": verdict_for(r, floors)[0],
                        "failed": r.failed,
                    }
                    for r in results
                ],
                indent=2,
            ) + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(_WORK, ignore_errors=True)

    # Non-zero when a suite could not be measured at all. A component merely
    # below its floor only counts under --enforce, which nothing passes yet:
    # the floors are there to be read, not yet to stop a build.
    if any(r.failed for r in results):
        return 1
    if args.enforce and any(verdict_for(r, floors)[0] == "BELOW" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
