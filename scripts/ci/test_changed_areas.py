# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the CI step selection in ``changed_areas.py``.

Three groups:

* the selection itself — the cases the rules exist for, and the fail-safe ones
  (unknown paths, shared configuration, an empty list, a diff that cannot be computed);
* the cross-suite reads — a file one suite's tests read from another area must run
  that suite, and both sides of every copy held in sync by a contract test must run
  the test that compares them;
* the two CI configurations — every gated step names a real selection key, every key
  is used, a missing selection runs the step, and both platforms call this script.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CODE_CHECKS = REPO_ROOT / ".github" / "workflows" / "code-checks.yml"
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"


def _load():
    spec = importlib.util.spec_from_file_location("changed_areas", HERE / "changed_areas.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["changed_areas"] = module
    spec.loader.exec_module(module)
    return module


ca = _load()

#: The steps a documentation-only change runs: the three cheap Python suites that read
#: documentation pages.
DOC_READERS = {"ai_stack", "pr_review", "demo_recorder"}


def ran(paths: list[str]) -> set[str]:
    selection = ca.select(paths)
    assert not selection.full, selection.reason
    return {key for key, run in selection.steps.items() if run}


# --- the selection ------------------------------------------------------------------


@pytest.mark.parametrize(
    "paths",
    [
        ["CHANGELOG.md"],
        ["docs/configuration-guide.md", "CHANGELOG.md"],
        ["images/diagram.png", "README.md", "lma-ai-stack/README.md"],
        ["docs-site/astro.config.mjs", "threat-modeling/README.md"],
    ],
)
def test_a_documentation_change_runs_only_the_suites_that_read_documentation(paths) -> None:
    assert ca.docs_only(paths)
    steps = ran(paths)
    assert steps <= DOC_READERS | {"vp_template"}, steps
    assert {"pr_review", "ai_stack"} <= steps


def test_a_changelog_only_change_runs_nothing_heavy() -> None:
    assert ran(["CHANGELOG.md"]) == DOC_READERS


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/code-checks.yml",
        ".github/workflows/ai-pr-review.yml",
        ".gitlab-ci.yml",
        "scripts/ci/changed_areas.py",
        "Makefile",
        ".nvmrc",
        "VERSION",
        "package-lock.json",
        "lma-ai-stack/pyproject.toml",
        "lma-ai-stack/conftest.py",
        "scripts/testing/no_real_aws.py",
    ],
)
def test_shared_configuration_runs_every_check(path: str) -> None:
    selection = ca.select(["CHANGELOG.md", path])
    assert selection.full, f"{path} must run every check"
    assert all(selection.steps.values())


def test_an_unclassified_path_runs_every_check() -> None:
    selection = ca.select(["docs/INDEX.md", "a-new-top-level-dir/thing.py"])
    assert selection.full
    assert "unknown" in selection.reason
    assert selection.areas["unknown"] == ["a-new-top-level-dir/thing.py"]


def test_an_empty_file_list_runs_every_check() -> None:
    assert ca.select([]).full
    assert ca.select(["", "  "]).full


def test_a_full_run_by_request_runs_every_check() -> None:
    selection = ca.select(None, full_reason="a push to develop")
    assert selection.full and all(selection.steps.values())
    assert selection.reason == "a push to develop"


def test_a_ui_change_does_not_run_the_other_stacks() -> None:
    steps = ran(["lma-ai-stack/source/ui/src/components/call-panel/CallPanel.jsx"])
    assert "ui" in steps and "ai_stack" in steps
    assert not steps & {"vp", "transcriber", "browser_extension", "asr", "typescript", "cfn_lint"}


def test_a_vp_backend_change_runs_the_vp_suites_and_not_the_ui() -> None:
    steps = ran(["lma-virtual-participant-stack/backend/src/teams.ts"])
    assert {"vp", "typescript", "vp_template", "ai_stack"} <= steps
    assert not steps & {"ui", "browser_extension", "asr", "lambdas"}


def test_a_layer_change_runs_every_lambda_suite_and_the_static_tests() -> None:
    steps = ran(["lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/codec.py"])
    assert {"lambdas", "python_lint", "ai_stack"} <= steps
    assert not steps & {"ui", "vp", "transcriber", "browser_extension"}


def test_the_sdk_and_cli_run_their_dependants() -> None:
    steps = ran(["lib/lma_sdk/lma_sdk/client.py"])
    assert {"sdk_cli", "ai_stack", "vp_template", "integ_plumbing"} <= steps


def test_a_template_change_runs_cfn_lint_and_the_template_readers() -> None:
    steps = ran(["lma-main.yaml"])
    assert {"cfn_lint", "ai_stack", "vp_template", "sdk_cli", "asr", "lambdas"} <= steps
    assert "ui" not in steps


def test_the_browser_extension_runs_alone() -> None:
    assert ran(["lma-browser-extension-stack/src/App.tsx"]) == {"browser_extension"}


def test_files_no_step_covers_run_nothing() -> None:
    assert (
        ran(["lma-desktop-capture-app-stack/source/main.js", "utilities/load-simulator/x.py"])
        == set()
    )


def test_a_rename_out_of_an_area_counts_against_both_areas() -> None:
    # changed_files passes --no-renames, so a move lists both paths.
    areas = ca.classify(["docs/x.md", "lma-ai-stack/source/ui/src/x.md"])
    assert {"docs", "ui"} <= areas.keys()


def test_docs_only_is_decided_by_the_primary_area() -> None:
    # It also runs the VP tests, which read it, but it is still documentation.
    assert ca.docs_only(["docs/virtual-participant.md"])
    assert not ca.docs_only(["docs/x.md", "lma-ai-stack/source/ui/src/App.jsx"])
    assert not ca.docs_only(["CLAUDE.md"]), "instruction files are not documentation"
    assert not ca.docs_only(
        ["lma-llm-template-setup-stack/source/lambda_functions/TroubleshootingAgentSystemPrompt.md"]
    ), "a prompt shipped by a stack is code, whatever its extension"


# --- cross-suite reads and copies held in sync ----------------------------------------


@pytest.mark.parametrize(
    ("path", "reader"),
    [
        # The transcriber compares its detector with the VP's copy.
        ("lma-virtual-participant-stack/backend/src/keyframe-detector.ts", "transcriber"),
        ("lma-websocket-transcriber-stack/source/app/src/videokeyframes.ts", "transcriber"),
        # configuration-store.ts: both TypeScript copies and the Python half are held
        # together by lma-ai-stack/test/test_configuration_store_contract.py.
        (
            "lma-websocket-transcriber-stack/source/app/src/config/configuration-store.ts",
            "ai_stack",
        ),
        ("lma-virtual-participant-stack/backend/src/config/configuration-store.ts", "ai_stack"),
        ("lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/store.py", "ai_stack"),
        # key_in_folder copies (test_key_in_folder_copies_agree.py) and bedrock_compat
        # copies (test_bedrock_compat.py), including the meeting-assist one.
        ("lma-ai-stack/source/lambda_functions/video_keyframes_resolver/index.py", "ai_stack"),
        ("lma-ai-stack/source/lambda_functions/consent_evidence_verifier/index.py", "ai_stack"),
        ("lma-meetingassist-setup-stack/src/bedrock_compat.py", "ai_stack"),
        # The layer's speaker-mode rules and their UI copy.
        ("lma-ai-stack/source/ui/src/components/common/speaker-mode-rules.json", "lambdas"),
        ("lma-ai-stack/source/ui/src/graphql/queries/onUpdateCall.js", "lambdas"),
        # The VP's inference catalogue copy, compared with the layer's.
        ("lma-virtual-participant-stack/backend/src/bedrock-inference-catalog.json", "ai_stack"),
        # The VP unit tests read these.
        ("docs/virtual-participant.md", "vp"),
        (
            "lma-ai-stack/source/ui/src/components/virtual-participant-layout/ManualActionAlerts.jsx",
            "vp",
        ),
        ("lma-ai-stack/deployment/lma-ai-stack.yaml", "vp"),
        (
            "lma-ai-stack/source/lambda_functions/configuration_seeder/defaults/MeetingConsent.json",
            "vp",
        ),
        (
            "lma-ai-stack/source/lambda_functions/configuration_seeder/defaults/MeetingConsent.json",
            "transcriber",
        ),
        (
            "lma-llm-template-setup-stack/source/lambda_functions/TroubleshootingAgentSystemPrompt.md",
            "vp",
        ),
        (
            "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/schemas/meeting_consent.json",
            "transcriber",
        ),
        # The VP static tests read the README's Launch Stack table and these Lambdas.
        ("README.md", "vp_template"),
        ("lma-ai-stack/source/lambda_functions/vnc_edge_token/index.py", "vp_template"),
        ("lma-ai-stack/source/appsync/schema.graphql", "vp_template"),
        # The ASR Lambdas read the runtime catalogue; the bundle check reads lma-main.
        ("lma-asr-microvm-stack/source/catalog.json", "lambdas"),
        ("lma-main.yaml", "asr"),
        # Shared fixtures: the UI and the Python suites both read them.
        ("test-fixtures/speaker-modes.json", "ui"),
        ("test-fixtures/speaker-modes.json", "lambdas"),
        ("test-fixtures/google-meet-inputs.json", "sdk_cli"),
        # The review harness's false-positive test reads every docs page, and the demo
        # recorder's tests read the agent instructions and the demo page.
        ("docs/monitoring.md", "pr_review"),
        ("CLAUDE.md", "pr_review"),
        ("CLAUDE.md", "demo_recorder"),
        (".claude/skills/product-demo.md", "demo_recorder"),
        ("docs/demo-videos.md", "demo_recorder"),
    ],
)
def test_a_file_another_suite_reads_runs_that_suite(path: str, reader: str) -> None:
    assert reader in ran([path]), f"{path} must run {reader}"


def test_every_rule_and_step_names_a_known_area() -> None:
    for rule in ca.PRIMARY_RULES + ca.EXTRA_RULES:
        assert set(rule.areas) <= ca.AREAS.keys(), rule
    for key, step in ca.STEPS.items():
        assert step.areas <= ca.AREAS.keys(), key
        assert not step.areas & ca.FULL_RUN_AREAS, f"{key}: a full-run area is implicit"


def test_every_area_runs_something_or_is_declared_to_run_nothing() -> None:
    covered = set().union(*(step.areas for step in ca.STEPS.values()))
    idle = ca.AREAS.keys() - covered - ca.FULL_RUN_AREAS
    assert idle == {"no_ci_coverage"}, idle


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("docs/**", "docs/a/b.md", True),
        ("docs/**", "docsx/a.md", False),
        ("**/README.md", "README.md", True),
        ("**/README.md", "a/b/README.md", True),
        ("lma-ai-stack/*", "lma-ai-stack/pyproject.toml", True),
        ("lma-ai-stack/*", "lma-ai-stack/test/x.py", False),
        ("scripts/ux_recorder*.py", "scripts/ux_recorder_cdp.py", True),
        ("Makefile", "lma-ai-stack/ml-stacks/Makefile", False),
    ],
)
def test_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert ca.Rule(pattern, ("docs",)).matches(path) is expected


# --- output formats and the command line ----------------------------------------------


def test_github_outputs_and_shell_assignments_name_every_step() -> None:
    selection = ca.select(["CHANGELOG.md"])
    outputs = dict(line.split("=", 1) for line in ca.github_outputs(selection).splitlines())
    assert outputs["ui"] == "false" and outputs["pr_review"] == "true"
    assert outputs["full"] == "false" and outputs["areas"] == "docs"
    assert ca.STEPS.keys() <= outputs.keys()
    shell = ca.shell_assignments(selection)
    assert "CI_RUN_UI=false" in shell and "CI_RUN_PR_REVIEW=true" in shell
    assert all(re.fullmatch(r"CI_RUN_[A-Z_]+=(true|false)", line) for line in shell.splitlines())


def test_the_summary_says_what_ran_and_why() -> None:
    text = ca.render_summary(ca.select(["CHANGELOG.md"]))
    assert "| Advisory AI review tests | yes | changed: `docs` |" in text
    assert "| React UI lint and tests | skipped | nothing it covers changed |" in text
    assert "`CHANGELOG.md`" in text
    full = ca.render_summary(ca.select(None, full_reason="a push to develop"))
    assert "Every check runs:** a push to develop" in full


def _git(cwd: Path, *args: str) -> str:
    # Identity and signing are set here so a developer's global configuration (a
    # signing key, a required identity) cannot fail the fixture.
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git {args}: {result.stderr}"
    return result.stdout.strip()


def test_the_command_line_lists_a_real_diff(tmp_path, monkeypatch) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "CHANGELOG.md").write_text("a\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "CHANGELOG.md").write_text("b\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("x\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "head")
    monkeypatch.chdir(tmp_path)

    out, summary, shell = tmp_path / "out", tmp_path / "summary.md", tmp_path / "ci.env"
    assert (
        ca.main(
            [
                "--base",
                base,
                "--head",
                "HEAD",
                "--github-output",
                str(out),
                "--summary",
                str(summary),
                "--shell",
                str(shell),
            ]
        )
        == 0
    )
    assert "ui=false" in out.read_text() and "full=false" in out.read_text()
    assert "`docs/x.md`" in summary.read_text()
    assert "CI_RUN_UI=false" in shell.read_text()


def test_a_diff_that_cannot_be_computed_runs_everything(tmp_path, monkeypatch) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("x")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "only")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    # HEAD^1 does not exist: the shape of a checkout that is not a merge commit.
    ca.main(["--base", "HEAD^1", "--head", "HEAD", "--github-output", str(out)])
    assert "full=true" in out.read_text()
    assert "ui=true" in out.read_text()


# --- the two CI configurations --------------------------------------------------------


@pytest.fixture(scope="module")
def github_steps() -> list[dict]:
    workflow = yaml.safe_load(CODE_CHECKS.read_text())
    job = workflow["jobs"]["code-checks"]
    assert job["name"] == "Lint and unit tests", (
        "the job name is the required status check; renaming it leaves branch "
        "protection waiting for a check that never reports"
    )
    assert "if" not in job, "the required check must report on every run"
    return job["steps"]


#: Steps that run unconditionally: setup, the selection itself, and this suite.
UNGATED = {
    "Check out the repository",
    "Set up Python",
    "Set up Node.js",
    "Cache pip downloads",
    "Select the checks this change needs",
    "Install Python tooling",
    "Install LMA SDK and CLI (dev extras)",
    "CI step-selection tests",
}


def test_every_github_step_is_gated_on_a_real_key_and_runs_when_unset(github_steps) -> None:
    used: set[str] = set()
    for step in github_steps:
        name = step["name"]
        condition = step.get("if", "")
        if name in UNGATED:
            assert not condition, f"{name} must always run"
            continue
        match = re.fullmatch(r"steps\.areas\.outputs\.(\w+) != 'false'", str(condition))
        assert match, (
            f"{name!r} needs `if: steps.areas.outputs.<key> != 'false'`. The `!= "
            f"'false'` form is deliberate: a missing or misspelt output runs the step."
        )
        assert match.group(1) in ca.STEPS, f"{name}: unknown key {match.group(1)}"
        used.add(match.group(1))
    assert used == ca.STEPS.keys(), f"keys no step uses: {ca.STEPS.keys() - used}"


def test_the_github_selection_is_selective_only_for_pull_requests(github_steps) -> None:
    step = next(s for s in github_steps if s["name"] == "Select the checks this change needs")
    assert step["id"] == "areas"
    body = step["run"]
    assert "scripts/ci/changed_areas.py" in body
    assert "pull_request" in body and "--base HEAD^1" in body and "--all" in body
    checkout = next(s for s in github_steps if s["name"] == "Check out the repository")
    assert checkout["with"]["fetch-depth"] >= 2, "HEAD^1 of the merge commit must be fetched"


def test_every_gitlab_step_is_gated_on_a_real_key() -> None:
    job = yaml.safe_load(GITLAB_CI.read_text())["code_checks"]
    script = "\n".join(job["script"])
    assert "scripts/ci/changed_areas.py" in script
    assert "CI_MERGE_REQUEST_DIFF_BASE_SHA" in script and "--all" in script
    used = set(re.findall(r"ci_run ([A-Z_]+)", script))
    assert {key.upper() for key in ca.STEPS} == used, used
    # Every make target and npm command is inside a ci_run block, apart from the
    # always-run CI tooling tests.
    for line in job["script"]:
        if "make " in line or "npm " in line:
            assert "ci_run " in line or "test-ci-tooling" in line, line
