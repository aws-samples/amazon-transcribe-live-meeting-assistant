#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Measure line coverage for every test suite in the repository and print a table.

Run via `make test-coverage`. This **reports**; it does not enforce. There is
deliberately no threshold and the exit code reflects only whether the suites
themselves ran — the point of this first step is to establish a baseline that a
threshold can later be argued from, not to fail anyone's build today.

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
    """The shared Lambda layer, which has no test suite of its own.

    Included on purpose: it holds `normalize_transcript_segments` and the
    transcript TTL handling, and leaving it out of the table would hide the
    largest untested Python surface in the repository behind its absence.
    """
    root = REPO / "lma-ai-stack" / "source" / "lambda_layers"
    result = Result(name="Lambda layers", language="python")
    sources = _python_sources([root])
    result.files_unimported = len(sources)
    result.unimported_counted_in_total = True
    result.total = sum(_statements_in(path) for path in sources)
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


def print_table(results: list[Result]) -> None:
    """Print the per-component table, the two language totals, and the caveats."""
    headers = ("Component", "Lang", "Lines", "Covered", "Total", "Files", "No data")
    rows = []
    for item in results:
        if item.failed:
            rows.append((item.name, item.language, "n/a", "—", "—", "—", "—"))
            continue
        rows.append((
            item.name,
            item.language,
            f"{item.percent:.1f}%",
            f"{item.covered}",
            f"{item.total}",
            f"{item.files_measured}",
            f"{item.files_unimported}{'' if item.unimported_counted_in_total else '*'}",
        ))

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
            python_covered, python_total, "", "",
        )))
    if node_total:
        print(line((
            "Node (all)", "node", f"{100 * node_covered / node_total:.1f}%",
            node_covered, node_total, "", "",
        )))

    print()
    print("Lines  = line (statement) coverage. Files = files the suite loaded.")
    print("No data = source files no test imported. These are counted as zero in")
    print("          the Python totals; for Node (*) they are excluded from the")
    print("          percentage, which is therefore optimistic.")
    print()
    print("Reported, not enforced: there is no threshold and this cannot fail a")
    print("build. Establishing the baseline comes first.")

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
    args = parser.parse_args(argv)

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

        print_table(results)

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
                        "failed": r.failed,
                    }
                    for r in results
                ],
                indent=2,
            ) + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(_WORK, ignore_errors=True)

    # Non-zero only when a suite could not be measured — never on a low number.
    return 1 if any(r.failed for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
