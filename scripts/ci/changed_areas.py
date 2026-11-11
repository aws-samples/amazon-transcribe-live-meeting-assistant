#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Decide which steps of the "Lint and unit tests" job a change needs.

The job (``code-checks.yml`` on GitHub, ``code_checks`` in ``.gitlab-ci.yml``) used to
run every suite on every pull request, so a one-line CHANGELOG fix paid for the UI
build, three TypeScript builds, the ASR runtime venv and the browser extension. This
module maps the files a change touches to **areas**, and each step of the job to the
areas whose changes require it. Both CI configurations call it, so the two cannot
choose differently.

Three rules keep it safe to rely on:

* **Unknown means everything.** A path that matches no rule below puts the change in
  the ``unknown`` area, and that runs every step. So a new top-level directory costs a
  full run until somebody classifies it, never a silently skipped suite.
* **Shared code means everything, or every dependant.** The CI configuration itself
  (including this file), the Makefile, the Node pin, the root lockfiles and the AI
  stack's Python tool configuration run every step. Shared libraries (the Lambda
  layers, the SDK/CLI, ``test-fixtures/``) are areas whose steps include every suite
  that imports or reads them.
* **Only pull requests are selective.** A push to ``develop`` or ``main``, a manual
  run and a schedule run every step (the CI configurations pass ``--all``), so the
  branch everybody builds on is always fully covered. A cross-suite read that this
  table misses can therefore delay a failure until the merge, never hide it.

How a step's areas were chosen
------------------------------
A step's areas are the areas of the code it builds or tests **plus every area its
tests read files from**. This repository's suites read across stacks a great deal —
the AI stack static tests read nearly every stack and several docs pages, the VP unit
tests read UI components and ``docs/virtual-participant.md``, the transcriber tests
read the VP's key-frame detector — and those reads are what :data:`EXTRA_RULES` and
the wider trigger sets in :data:`STEPS` encode. A test that starts reading a file in
another area needs a line here; ``test_changed_areas.py`` pins the known reads.

Usage
-----
    # a pull request on GitHub: the checkout is the PR's merge commit
    python3 scripts/ci/changed_areas.py --base HEAD^1 --head HEAD \\
        --github-output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"

    # a GitLab merge-request pipeline
    python3 scripts/ci/changed_areas.py --base "$CI_MERGE_REQUEST_DIFF_BASE_SHA" \\
        --head HEAD --fetch origin --shell ci-areas.env

    # everything (develop/main pushes, manual runs)
    python3 scripts/ci/changed_areas.py --all "a push to develop" --shell ci-areas.env

    # try it on a list of paths
    git diff --name-only origin/develop... | python3 scripts/ci/changed_areas.py --files-from -
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

#: Areas, with the one-line description the job summary shows.
AREAS: dict[str, str] = {
    "docs": "documentation, CHANGELOG, READMEs, images and the docs site",
    "ui": "React UI (lma-ai-stack/source/ui)",
    "lambdas": "Python Lambda functions and the meeting-assist stack",
    "layers": "Lambda layers shared by the Lambda functions",
    "appsync": "AppSync schema and resolvers",
    "vp": "Virtual Participant backend",
    "vp_template": "Virtual Participant template, Lambdas and static tests",
    "asr": "ASR MicroVM stack",
    "transcriber": "WebSocket transcriber stack",
    "browser_extension": "browser extension",
    "websocket_client": "utilities/websocket-client sample client",
    "sdk_cli": "LMA SDK and CLI (lib/)",
    "templates": "CloudFormation templates",
    "config_stacks": "LLM template, chat button and Nova Sonic configuration stacks",
    "test_fixtures": "test-fixtures/ shared by the UI and the Python suites",
    "integ_plumbing": "integration-test machinery (integ-tests/)",
    "pr_review": "advisory AI review harness (scripts/pr_review)",
    "agent_instructions": "CLAUDE.md and the .claude/ skill files",
    "demo_recorder": "product demo recorder and its helpers",
    "python_tooling": "Python generators the AI stack static tests run",
    "ai_stack_tests": "AI stack static tests (lma-ai-stack/test)",
    "no_ci_coverage": "files no step of this job builds or tests",
    "workflow": "the CI configuration itself",
    "shared": "build configuration every suite depends on",
    "unknown": "paths no rule classifies",
}

#: Any of these runs every step.
FULL_RUN_AREAS = frozenset({"workflow", "shared", "unknown"})


@dataclass(frozen=True)
class Rule:
    """A path pattern and the areas a match puts the change in.

    Patterns are globs over repository-relative POSIX paths: ``*`` matches within one
    path segment, ``**`` matches any number of segments (including none), and a
    pattern without wildcards matches that exact path.
    """

    pattern: str
    areas: tuple[str, ...]
    regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "regex", _glob_to_regex(self.pattern))

    def matches(self, path: str) -> bool:
        return self.regex.fullmatch(path) is not None


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def _rules(*entries: tuple[str, str | tuple[str, ...]]) -> tuple[Rule, ...]:
    return tuple(
        Rule(pattern, (areas,) if isinstance(areas, str) else tuple(areas))
        for pattern, areas in entries
    )


UI = "lma-ai-stack/source/ui"
FUNCTIONS = "lma-ai-stack/source/lambda_functions"
LAYERS = "lma-ai-stack/source/lambda_layers"
VP = "lma-virtual-participant-stack"
TRANSCRIBER = "lma-websocket-transcriber-stack"
ASR = "lma-asr-microvm-stack"

#: Ordered; the FIRST match is a path's primary area. Documentation comes first so a
#: README inside a stack is documentation rather than that stack's code.
PRIMARY_RULES: tuple[Rule, ...] = _rules(
    # --- documentation -------------------------------------------------------
    ("CHANGELOG.md", "docs"),
    ("CONTRIBUTING.md", "docs"),
    ("CODE_OF_CONDUCT.md", "docs"),
    ("TROUBLESHOOTING.md", "docs"),
    ("LICENSE", "docs"),
    ("NOTICE", "docs"),
    ("THIRD-PARTY-LICENSES.txt", "docs"),
    ("**/README.md", "docs"),
    ("**/README_*.md", "docs"),
    ("docs/**", "docs"),
    ("images/**", "docs"),
    ("docs-site/**", "docs"),
    ("threat-modeling/**", "docs"),
    # The AI stack's redirect stubs, its images and its PR template.
    ("lma-ai-stack/*.md", "docs"),
    ("lma-ai-stack/images/**", "docs"),
    ("lma-ai-stack/.github/**", "docs"),
    # --- the CI configuration and build configuration: everything ------------
    (".github/workflows/**", "workflow"),
    (".github/ISSUE_TEMPLATE/**", "docs"),
    (".gitlab-ci.yml", "workflow"),
    ("scripts/ci/**", "workflow"),
    ("Makefile", "shared"),
    (".nvmrc", "shared"),
    ("VERSION", "shared"),
    ("package.json", "shared"),
    ("package-lock.json", "shared"),
    ("publish.sh", "shared"),
    ("aws-release.sh", "shared"),
    (".gitmodules", "shared"),
    # The no-AWS guard every Python suite loads, and the strands stand-in.
    ("scripts/testing/**", "shared"),
    # pyproject.toml (ruff + pytest rootdir), conftest.py, lint configs, the AI
    # stack's own Makefile and requirements.
    ("lma-ai-stack/*", "shared"),
    ("lma-ai-stack/requirements/**", "shared"),
    # --- AI stack ------------------------------------------------------------
    (f"{UI}/**", "ui"),
    (f"{FUNCTIONS}/**", "lambdas"),
    (f"{LAYERS}/**", "layers"),
    ("lma-ai-stack/source/appsync/**", "appsync"),
    ("lma-ai-stack/deployment/**", "templates"),
    ("lma-ai-stack/test/**", "ai_stack_tests"),
    ("lma-ai-stack/ml-stacks/**", "templates"),
    ("lma-meetingassist-setup-stack/template.yaml", ("lambdas", "templates")),
    ("lma-meetingassist-setup-stack/**", "lambdas"),
    # --- Virtual Participant -------------------------------------------------
    (f"{VP}/backend/**", "vp"),
    (f"{VP}/template.yaml", ("vp_template", "templates")),
    (f"{VP}/lambda_functions/**", "vp_template"),
    (f"{VP}/test/**", "vp_template"),
    # --- transcriber, ASR, extension, clients --------------------------------
    (f"{TRANSCRIBER}/deployment/**", ("transcriber", "templates")),
    (f"{TRANSCRIBER}/**", "transcriber"),
    (f"{ASR}/template.yaml", ("asr", "templates")),
    (f"{ASR}/lambda_functions/**", "lambdas"),
    (f"{ASR}/**", "asr"),
    ("lma-browser-extension-stack/template.yaml", ("browser_extension", "templates")),
    ("lma-browser-extension-stack/**", "browser_extension"),
    ("utilities/websocket-client/**", "websocket_client"),
    # --- templates and configuration stacks ----------------------------------
    ("lma-main.yaml", "templates"),
    ("lma-vpc-stack/**", "templates"),
    ("lma-cognito-stack/**", "templates"),
    ("lma-bedrockagent-stack/**", "templates"),
    ("lma-bedrockkb-stack/**", "templates"),
    ("iam-roles/**", "templates"),
    ("lma-desktop-capture-app-stack/template.yaml", "templates"),
    ("lma-llm-template-setup-stack/**", "config_stacks"),
    ("lma-chat-button-config-stack/**", "config_stacks"),
    ("lma-nova-sonic-config-stack/**", "config_stacks"),
    # --- shared libraries and fixtures ---------------------------------------
    ("lib/**", "sdk_cli"),
    ("test-fixtures/**", "test_fixtures"),
    ("integ-tests/**", "integ_plumbing"),
    # --- review harness, agent instructions, demo recorder, tooling ----------
    ("scripts/pr_review/**", "pr_review"),
    ("CLAUDE.md", "agent_instructions"),
    ("AGENTS.md", "agent_instructions"),
    (".claude/**", "agent_instructions"),
    (".cline/**", "agent_instructions"),
    (".clinerules", "agent_instructions"),
    ("scripts/ux_recorder*.py", "demo_recorder"),
    ("scripts/ux_test_session.py", "demo_recorder"),
    ("scripts/demo_storyboards.yaml", "demo_recorder"),
    ("scripts/requirements-ux-recorder.txt", "demo_recorder"),
    ("scripts/meeting_guest/**", "demo_recorder"),
    ("scripts/tests/**", "demo_recorder"),
    ("scripts/cloudformation_parameters_doc.py", "python_tooling"),
    ("scripts/bedrock_inference_catalog.py", "python_tooling"),
    # --- tracked, but built or tested by nothing in this job -----------------
    # Each of these is a deliberate "nothing to run" rather than an unknown. The
    # desktop capture app, the load simulator and the placeholder packages have no
    # suite here; the SRT files feed the separate security-review job; the rest are
    # developer scripts.
    ("lma-desktop-capture-app-stack/**", "no_ci_coverage"),
    ("utilities/**", "no_ci_coverage"),
    ("pypi-placeholder-packages/**", "no_ci_coverage"),
    ("amazon-quick-desktop-skills-pack/**", "no_ci_coverage"),
    (".github/dependabot.yml", "no_ci_coverage"),
    (".srt/**", "no_ci_coverage"),
    ("scripts/srt/**", "no_ci_coverage"),
    (".semgrepignore", "no_ci_coverage"),
    (".ash.yaml", "no_ci_coverage"),
    (".gitignore", "no_ci_coverage"),
    ("**/.gitignore", "no_ci_coverage"),
    ("scripts/coverage_report.py", "no_ci_coverage"),
    ("scripts/coverage_floors.json", "no_ci_coverage"),
    ("scripts/mutate.sh", "no_ci_coverage"),
    ("scripts/_mutate_helper.py", "no_ci_coverage"),
    ("scripts/verify.sh", "no_ci_coverage"),
    ("scripts/generate_commit_message.sh", "no_ci_coverage"),
    ("scripts/dev-push-vp.sh", "no_ci_coverage"),
)

#: Every match ADDS its areas. These are the cross-suite reads: one suite's tests
#: reading a file that belongs to another area, so a change to that file must run the
#: reading suite too. Each line names the reader.
EXTRA_RULES: tuple[Rule, ...] = _rules(
    # The transcriber's tests compare its key-frame detector with the VP's copy and
    # read the consent defaults and schema it mirrors.
    (f"{VP}/backend/src/keyframe-detector.ts", "transcriber"),
    (f"{FUNCTIONS}/configuration_seeder/defaults/**", ("transcriber", "vp")),
    (f"{LAYERS}/configuration_layer/lma_config/schemas/**", "transcriber"),
    # The VP unit tests read these UI components, the AI stack template, the
    # troubleshooting prompt and the VP doc's list of exit triggers.
    (f"{UI}/src/components/virtual-participant-layout/**", "vp"),
    (f"{UI}/src/components/configuration/inference-geography-cases.json", "vp"),
    ("lma-ai-stack/deployment/lma-ai-stack.yaml", ("vp", "lambdas", "vp_template")),
    ("lma-llm-template-setup-stack/**", "vp"),
    ("docs/virtual-participant.md", "vp"),
    # The Lambda and layer tests read the UI's GraphQL subscriptions and its shared
    # helpers (speaker modes, channels, summary state) to keep the two sides equal.
    (f"{UI}/src/graphql/**", "lambdas"),
    (f"{UI}/src/components/common/**", "lambdas"),
    # The ASR Lambdas' tests read the runtime's catalogue.
    (f"{ASR}/source/catalog.json", "lambdas"),
    (f"{ASR}/template.yaml", "lambdas"),
    # The VP static tests read these Lambdas, the schema and the Launch Stack table.
    (f"{FUNCTIONS}/microvm_vnc_token/**", "vp_template"),
    (f"{FUNCTIONS}/vnc_edge_token/**", "vp_template"),
    (f"{FUNCTIONS}/edge_auth_deployer/**", "vp_template"),
    ("lma-ai-stack/source/appsync/schema.graphql", "vp_template"),
    ("README.md", "vp_template"),
    # lma-main.yaml is read by the ASR bundle check, the configuration seeder's
    # tests and the SDK's publish tests. (The demo recorder's session helper reads it
    # too; that step takes the whole `templates` area.)
    ("lma-main.yaml", ("asr", "lambdas", "sdk_cli")),
)


@dataclass(frozen=True)
class Step:
    """One gated step (or group of steps) of the job, and the areas that need it."""

    title: str
    areas: frozenset[str]


def _step(title: str, *areas: str) -> Step:
    return Step(title, frozenset(areas))


#: Keyed by the name both CI configurations use: the GitHub step output
#: ``steps.areas.outputs.<key>`` and the GitLab variable ``CI_RUN_<KEY>``.
STEPS: dict[str, Step] = {
    "cfn_lint": _step("Lint CloudFormation templates", "templates", "config_stacks"),
    "python_lint": _step(
        "Lint Python (ruff)",
        "lambdas",
        "layers",
        "pr_review",
        "demo_recorder",
        "python_tooling",
    ),
    "sdk_cli": _step(
        "LMA SDK and CLI unit tests", "sdk_cli", "templates", "config_stacks", "test_fixtures"
    ),
    "lambdas": _step(
        "Lambda function unit tests",
        "lambdas",
        "layers",
        "appsync",
        "config_stacks",
        "test_fixtures",
    ),
    "ui": _step("React UI lint and tests", "ui", "test_fixtures"),
    "typescript": _step("TypeScript build checks", "vp", "transcriber", "websocket_client"),
    "vp": _step("Virtual Participant unit tests", "vp"),
    "vp_template": _step(
        "Virtual Participant template tests", "vp", "vp_template", "sdk_cli", "templates"
    ),
    # Reads across nearly every stack, the Makefile and several docs pages, and takes
    # under a minute, so it runs for anything that is not wholly outside its reach.
    "ai_stack": _step(
        "AI stack static tests",
        "ai_stack_tests",
        "ui",
        "lambdas",
        "layers",
        "appsync",
        "vp",
        "vp_template",
        "transcriber",
        "asr",
        "templates",
        "config_stacks",
        "sdk_cli",
        "test_fixtures",
        "python_tooling",
        "docs",
    ),
    "integ_plumbing": _step("Integration-test plumbing tests", "integ_plumbing", "sdk_cli"),
    "asr": _step("ASR MicroVM runtime tests", "asr"),
    # Its false-positive test reads every docs page, so documentation runs it.
    # Both read CLAUDE.md and the skill files; the review harness's false-positive
    # test also reads every docs page, and the demo recorder checks its storyboard
    # against the demo page and the docs index.
    "pr_review": _step("Advisory AI review tests", "pr_review", "agent_instructions", "docs"),
    "demo_recorder": _step(
        "Demo recorder tests", "demo_recorder", "agent_instructions", "docs", "templates"
    ),
    "transcriber": _step("WebSocket transcriber tests and smoke test", "transcriber"),
    "browser_extension": _step("Browser extension tests and build", "browser_extension"),
}


def primary_area(path: str) -> tuple[str, ...]:
    """The areas of the first rule that matches ``path``, or ``("unknown",)``."""
    for rule in PRIMARY_RULES:
        if rule.matches(path):
            return rule.areas
    return ("unknown",)


def areas_for(path: str) -> set[str]:
    """Every area a change to ``path`` puts the change in."""
    found = set(primary_area(path))
    for rule in EXTRA_RULES:
        if rule.matches(path):
            found.update(rule.areas)
    return found


def classify(paths: Iterable[str]) -> dict[str, list[str]]:
    """Area -> the changed paths that put the change in it (sorted, de-duplicated)."""
    result: dict[str, list[str]] = {}
    for path in sorted({p.strip() for p in paths if p.strip()}):
        for area in areas_for(path):
            result.setdefault(area, []).append(path)
    return result


def docs_only(paths: Iterable[str]) -> bool:
    """True when every path is documentation by its PRIMARY area.

    Primary rather than all areas: ``docs/virtual-participant.md`` also runs the VP
    tests (they read it), but it is still a documentation change. This is the test the
    advisory AI review uses to decide that a new head needs no new model call.
    """
    listed = [p for p in (q.strip() for q in paths) if p]
    return all(primary_area(p) == ("docs",) for p in listed)


@dataclass
class Selection:
    """What to run, and why."""

    full: bool
    reason: str
    areas: dict[str, list[str]]
    steps: dict[str, bool]

    def triggers(self, key: str) -> list[str]:
        """The changed areas that made step ``key`` run."""
        return sorted(STEPS[key].areas & self.areas.keys())


def select(paths: Iterable[str] | None, full_reason: str = "") -> Selection:
    """Decide the steps for a change.

    ``paths=None`` (or a ``full_reason``) means a full run: a push to a long-lived
    branch, a manual run, or a change whose file list could not be computed.
    """
    if paths is None or full_reason:
        return Selection(True, full_reason or "no file list", {}, {key: True for key in STEPS})
    areas = classify(paths)
    if not areas:
        return Selection(
            True,
            "the change lists no files, which is unexpected, so every check runs",
            {},
            {key: True for key in STEPS},
        )
    forcing = sorted(FULL_RUN_AREAS & areas.keys())
    if forcing:
        return Selection(
            True,
            "files in " + ", ".join(f"`{a}`" for a in forcing) + " run every check",
            areas,
            {key: True for key in STEPS},
        )
    steps = {key: bool(step.areas & areas.keys()) for key, step in STEPS.items()}
    return Selection(False, "steps selected by the areas this change touches", areas, steps)


# --- rendering -----------------------------------------------------------------------

_MAX_LISTED = 5


def render_summary(selection: Selection) -> str:
    """Markdown for ``$GITHUB_STEP_SUMMARY`` and the job log."""
    lines = ["### Checks selected for this change", ""]
    if selection.full:
        lines += [f"**Every check runs:** {selection.reason}.", ""]
    else:
        ran = sum(selection.steps.values())
        lines += [
            f"**{ran} of {len(STEPS)} gated checks run**, chosen from the areas below "
            "by `scripts/ci/changed_areas.py`. Pushes to `develop` and `main` run "
            "everything.",
            "",
        ]
    if selection.areas:
        lines += ["| Area | Changed files |", "|---|---|"]
        for area in sorted(selection.areas):
            files = selection.areas[area]
            shown = ", ".join(f"`{f}`" for f in files[:_MAX_LISTED])
            if len(files) > _MAX_LISTED:
                shown += f" and {len(files) - _MAX_LISTED} more"
            lines.append(f"| `{area}` ({AREAS.get(area, area)}) | {shown} |")
        lines.append("")
    lines += ["| Check | Runs | Because |", "|---|---|---|"]
    for key, step in STEPS.items():
        if selection.full:
            because = "full run"
        elif selection.steps[key]:
            because = "changed: " + ", ".join(f"`{a}`" for a in selection.triggers(key))
        else:
            because = "nothing it covers changed"
        mark = "yes" if selection.steps[key] else "skipped"
        lines.append(f"| {step.title} | {mark} | {because} |")
    lines.append("")
    return "\n".join(lines)


def github_outputs(selection: Selection) -> str:
    """``key=value`` lines for ``$GITHUB_OUTPUT``."""
    out = [f"{key}={'true' if run else 'false'}" for key, run in selection.steps.items()]
    out.append(f"full={'true' if selection.full else 'false'}")
    out.append("areas=" + ",".join(sorted(selection.areas)))
    return "\n".join(out) + "\n"


def shell_assignments(selection: Selection) -> str:
    """``CI_RUN_<KEY>=true|false`` lines for a POSIX shell to source (GitLab)."""
    out = [
        f"CI_RUN_{key.upper()}={'true' if run else 'false'}" for key, run in selection.steps.items()
    ]
    return "\n".join(out) + "\n"


# --- git -----------------------------------------------------------------------------


class DiffUnavailable(Exception):
    """The changed-file list could not be computed; the caller runs everything."""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args], capture_output=True, text=True, check=False
    )


def changed_files(base: str, head: str, fetch_remote: str = "") -> list[str]:
    """``git diff --name-only base head``, fetching ``base`` first if it is absent.

    Both sides of a rename are listed (``--no-renames``), so a file moved out of an
    area counts against that area as well as the one it moved to.
    """
    for rev in (base, head):
        if _git("cat-file", "-e", f"{rev}^{{commit}}").returncode == 0:
            continue
        if fetch_remote:
            _git("fetch", "--quiet", "--depth=1", fetch_remote, rev)
        if _git("cat-file", "-e", f"{rev}^{{commit}}").returncode != 0:
            raise DiffUnavailable(f"{rev} is not available in this clone")
    result = _git("diff", "--name-only", "--no-renames", base, head)
    if result.returncode != 0:
        raise DiffUnavailable(f"git diff failed: {result.stderr.strip()[:200]}")
    return [line for line in result.stdout.splitlines() if line.strip()]


# --- command line --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--all", metavar="REASON", help="run every step, for this reason")
    source.add_argument("--base", help="diff from this revision (with --head)")
    source.add_argument(
        "--files-from", metavar="PATH", help="read changed paths from a file, or - for stdin"
    )
    parser.add_argument("--head", default="HEAD", help="diff to this revision (default HEAD)")
    parser.add_argument(
        "--fetch", metavar="REMOTE", default="", help="fetch a missing --base from REMOTE"
    )
    parser.add_argument("--github-output", type=Path, help="append step outputs to this file")
    parser.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    parser.add_argument("--shell", type=Path, help="write CI_RUN_<KEY> assignments to this file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.all:
        selection = select(None, full_reason=args.all)
    elif args.files_from:
        text = sys.stdin.read() if args.files_from == "-" else Path(args.files_from).read_text()
        selection = select(text.splitlines())
    else:
        try:
            selection = select(changed_files(args.base, args.head, args.fetch))
        except DiffUnavailable as error:
            selection = select(None, full_reason=f"the changed files could not be listed ({error})")

    summary = render_summary(selection)
    print(summary)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(github_outputs(selection))
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    if args.shell:
        args.shell.write_text(shell_assignments(selection), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
