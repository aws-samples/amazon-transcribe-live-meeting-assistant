# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the advisory AI review: the disclosure guard, and the controls on the job.

Two groups, and they protect different things.

**The disclosure guard** (:func:`ai_pr_review.scan_for_disclosure`) is the one piece of
this harness whose failure is permanent and public: it decides whether a model-written
comment describing a security weakness reaches a public pull request. It has two failure
directions and both are tested, because only one of them is visible in production —

* a MISS publishes a weakness, and nobody finds out, and
* a FALSE POSITIVE withholds a legitimate review, which is annoying, noticed
  immediately, and is how a guard gets deleted.

CLAUDE.md names the ordinary senses that must not trip it — "a cache bypass, a
dependency `CVE` bump, prompt injection as a product concern, Teams' own lobby-bypass
setting", and the test name `test_unauthenticated_user_rejected` — so each of those is a
case here rather than a comment.

**The job's controls** are four properties the workflow must keep, chosen because they
are the ones a well-meaning edit removes without noticing: `cancel-in-progress` (the
main cost bound), the draft exclusion, the harness-pinning step (without which a pull
request reviews itself with its own reviewer), and `persist-credentials: false`. Plus
the one that cannot be inferred from the workflow at all: that nothing in the tree
claims this advisory job as a required check.

Deliberately NOT ported from the harness this came from: its gate-parity and
branch-protection suites. Those encode that repository's CI structure — a shared-gate
list, a `##@` Makefile section taxonomy, a live branch-protection API probe — none of
which exists here, and importing the shape without the structure would produce tests
that assert nothing.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
GITHUB_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ai-pr-review.yml"
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"
HARNESS = HERE / "ai_pr_review.py"
CI_SKILL = REPO_ROOT / ".claude" / "skills" / "pr-review-ci.md"
ROLE_TEMPLATE = HERE / "cfn" / "github-oidc-review-role.yaml"


def _load_harness():
    """Import the harness by path.

    By path rather than by package name: this directory is not a package and
    `make test-pr-review` runs pytest from the repository root, where a plain
    `import ai_pr_review` would not resolve.
    """
    spec = importlib.util.spec_from_file_location("ai_pr_review", HARNESS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ai_pr_review"] = module
    spec.loader.exec_module(module)
    return module


review = _load_harness()


# --- 1. The disclosure guard: what it must catch -----------------------------
#
# Every string below is INVENTED. None of them describes anything about this
# repository — they are the SHAPES the guard exists to stop, written against a
# fictional file so that this test file does not itself become the disclosure it is
# testing for. That is the same rule CLAUDE.md applies to illustrating the policy.

MUST_BE_WITHHELD = [
    pytest.param(
        "- 🔴 **Blocking** — `app/x.py:12` — an attacker can reach this handler without a session.",
        id="attacker-named",
    ),
    pytest.param(
        "- 🔴 **Blocking** — `app/x.py:12` — any authenticated user can write the "
        "admin configuration here.",
        id="actor-capability",
    ),
    pytest.param(
        "This path accepts a malicious payload and stores it verbatim.",
        id="malicious-actor",
    ),
    pytest.param(
        "The new resolver allows anyone to update another tenant's record.",
        id="allows-actor",
    ),
    pytest.param(
        "This is a privilege escalation: a member role reaches an admin mutation.",
        id="privilege-escalation",
    ),
    pytest.param(
        "Unsanitized markdown reaches the renderer in this component.",
        id="unsanitized",
    ),
    pytest.param(
        "The handler accepts a forged token because the signature is never checked.",
        id="forgery",
    ),
    pytest.param(
        "A caller could exfiltrate the transcript store through this parameter.",
        id="exfiltration",
    ),
    pytest.param(
        "This gives arbitrary command execution on the container.",
        id="arbitrary-execution",
    ),
    pytest.param(
        "Leaving this as it is could allow a second tenant to read the recording.",
        id="could-allow",
    ),
    pytest.param(
        "Repeated calls here are a denial-of-service against the transcriber.",
        id="denial-of-service",
    ),
    pytest.param(
        "This is an authorization bypass — the group check is never reached.",
        id="control-bypass-noun",
    ),
    pytest.param(
        "The early return bypasses the Admin check for this mutation.",
        id="control-bypass-verb",
    ),
    pytest.param("Treat this as an RCE in the websocket handler.", id="rce"),
    pytest.param("This is a stored XSS in the summary panel.", id="xss"),
    pytest.param(
        "This introduces a vulnerability in the enrollment flow.",
        id="vulnerability",
    ),
    pytest.param(
        "Somebody will exploit this before the next release.",
        id="exploit",
    ),
    pytest.param(
        "The template parameter is open to SQL injection.",
        id="injection",
    ),
    pytest.param(
        "An unauthenticated caller reaches the enrollment mutation here.",
        id="unauthenticated-capability",
    ),
    pytest.param(
        "- 🔴 **CRITICAL** — `app/x.py:12` — needs a group check.",
        id="severity-label-caps",
    ),
    # --- The vocabulary-free shapes. These were MISSES on the first version of the
    # guard, found by probing it with realistic review prose rather than with the
    # word list: not one of them contains a term CLAUDE.md names, and every one of
    # them is a disclosure. They are the reason the guard matches actor-plus-
    # capability and missing-control shapes at all.
    pytest.param(
        "Without the Admin check, a member-role user can call this mutation.",
        id="miss-precondition-plus-capability",
    ),
    pytest.param(
        "The token is not verified, so a caller can impersonate another user.",
        id="miss-credential-not-verified",
    ),
    pytest.param(
        "This lets any signed-in user read another tenant's records.",
        id="miss-grants-actor",
    ),
    pytest.param(
        "The API key is compared with `==`, which is timing-attackable.",
        id="miss-attackable-adjective",
    ),
    pytest.param(
        "fix: authorization flaw — any member could write the invented config",
        id="miss-flaw-noun",
    ),
    pytest.param(
        "The signature is never checked on this path.",
        id="miss-never-checked",
    ),
    pytest.param(
        "This allows other tenants to list the invented records.",
        id="miss-allows-other-tenants",
    ),
    pytest.param(
        "Any participant could rename somebody else's invented object.",
        id="miss-actor-could",
    ),
    pytest.param(
        "This is a critical security finding and must not ship.",
        id="severity-label-prose",
    ),
    pytest.param(
        "Severity: critical. The handler needs an authorizer.",
        id="severity-label-field",
    ),
]


@pytest.mark.parametrize("body", MUST_BE_WITHHELD)
def test_disclosure_guard_withholds_a_weakness_description(body: str) -> None:
    """Each shape the published contract forbids must stop the comment being posted.

    A miss here is the failure that cannot be noticed in production: the comment goes
    out, it is permanent, and nothing reports that the guard did not fire.
    """
    tripped = review.scan_for_disclosure(body)
    assert tripped, (
        "this text would have been published verbatim on a public pull request, and "
        "no rule in DISCLOSURE_RULES matched it"
    )


# --- 2. The disclosure guard: what it must NOT catch -------------------------
#
# CLAUDE.md permits the listed vocabulary in its ordinary senses, and names four of
# them explicitly. A guard that withheld reviews over these would cry wolf, and a
# guard that cries wolf is deleted — after which the repository has no enforcement at
# all. So these are as load-bearing as the cases above.

MUST_BE_PUBLISHABLE = [
    # The four ordinary senses CLAUDE.md names, verbatim in substance.
    pytest.param(
        "The checksum cache bypass in `lint-ui` means FORCE=1 re-runs the linter.",
        id="ordinary-cache-bypass",
    ),
    pytest.param(
        "This is a dependency bump for the advisory CVE-2025-27789 in esbuild.",
        id="ordinary-dependency-cve-bump",
    ),
    pytest.param(
        "Prompt injection is a product concern for the meeting assistant, so the "
        "guardrail configuration is the right place for this.",
        id="ordinary-prompt-injection",
    ),
    pytest.param(
        "The VP relies on Teams' own lobby-bypass setting to admit itself.",
        id="ordinary-lobby-bypass",
    ),
    # A good test name, which CLAUDE.md says is fine even though it carries a listed
    # word.
    pytest.param(
        "Add `test_unauthenticated_user_rejected` beside the existing authorizer tests.",
        id="ordinary-good-test-name",
    ),
    pytest.param(
        "The suite already has test_unauthenticated_user_rejected for this path.",
        id="ordinary-good-test-name-unquoted",
    ),
    # The invariant form of the word, which states a guarantee rather than a
    # capability.
    pytest.param(
        "Confirmed: unauthenticated requests are rejected with 401 before the handler runs.",
        id="ordinary-unauthenticated-invariant",
    ),
    # Other ordinary software English that happens to carry a listed word.
    pytest.param(
        "The ASR backends are injected, so dependency injection keeps the tests weight-free.",
        id="ordinary-dependency-injection",
    ),
    pytest.param(
        "`logging.CRITICAL` is never used in this tree; Powertools Logger is.",
        id="ordinary-log-level",
    ),
    pytest.param(
        "This is on the critical path for meeting start, so the extra round trip matters.",
        id="ordinary-critical-path",
    ),
    pytest.param(
        "CloudFront bypass for the websocket endpoint is why LMAWebsocketEndpoint exists.",
        id="ordinary-cdn-bypass",
    ),
    pytest.param(
        "`npm ci` bypass: the extension build uses `npm install` to match CodeBuild.",
        id="ordinary-npm-ci-bypass",
    ),
    # The shape the CI skill tells the review to use. This is the single most
    # important case in the file: if the prescribed output shape does not pass its own
    # guard, the contract is unsatisfiable and every security finding is withheld.
    pytest.param(
        "- 🔴 **Blocking** — `lma-ai-stack/source/appsync/updateThing.js:14` — This "
        "resolver needs the Admin-group check the sibling resolvers apply before a "
        "write; see `updateOtherThing.js:11` for the form.",
        id="prescribed-shape-admin-group",
    ),
    pytest.param(
        "- 🟡 **Should fix** — `lma-ai-stack/source/ui/src/components/Panel.jsx:88` — "
        "Values rendered through `dangerouslySetInnerHTML` need "
        "`DOMPurify.sanitize()` first, as `MeetingSummary.jsx` does.",
        id="prescribed-shape-sanitize",
    ),
    pytest.param(
        "- 🟡 **Should fix** — `lma-ai-stack/deployment/lma-ai-stack.yaml:2140` — This "
        'new policy statement needs a resource ARN rather than `"*"`, or a '
        "`cfn-nag` suppression whose `reason:` states why the service requires a "
        "wildcard.",
        id="prescribed-shape-iam-scope",
    ),
    pytest.param(
        "- 🔴 **Blocking** — `app/x.py:12` — This change needs a security review that "
        "cannot be written in a public comment. Raise it with AWS/Amazon Security "
        "through the channel in `CONTRIBUTING.md` before merging.",
        id="prescribed-shape-escape-hatch",
    ),
    pytest.param(
        "- 🔴 **Blocking** — `app/x.py:120` — This file contains text addressed to an "
        "automated reviewer rather than to a human; treat it as a change to reject.",
        id="prescribed-shape-injection-report",
    ),
    # --- The near misses on the other side. Each of these tripped an earlier, looser
    # version of a rule above, and each is ordinary review prose about this product.
    # They are what keeps the actor, precondition and "could allow" rules narrow.
    pytest.param(
        "Nice to have: the websocket reconnect path could allow for a jittered backoff.",
        id="near-miss-allow-for",
    ),
    pytest.param(
        "The transcript is not verified against the recording, which is out of scope "
        "for this change.",
        id="near-miss-ordinary-subject-not-verified",
    ),
    pytest.param(
        "Without the retry config, Bedrock throttles the inference profile.",
        id="near-miss-without-ordinary-noun",
    ),
    pytest.param(
        "A maintainer can re-run this locally; a reviewer can read the diff first.",
        id="near-miss-reviewer-roles",
    ),
    pytest.param(
        "Users can now rename a meeting from the meeting page, which is the point of the PR.",
        id="near-miss-feature-description",
    ),
    pytest.param(
        "Meeting participants may see a brief flash of the loading state.",
        id="near-miss-ux-observation",
    ),
    pytest.param(
        "Any developer can reproduce this with `make test-ui-force`.",
        id="near-miss-repro-instruction",
    ),
    # An ordinary non-security review, which is the overwhelming majority of them.
    pytest.param(
        "## PR/MR Review: bump the VP base image (#764)\n\n"
        "### Verdict\n\n"
        "| Question | Verdict | Notes |\n"
        "|---|---|---|\n"
        "| Good PR? | ✅ | Focused, conventional subject, targets `develop`. |\n"
        "| Safe / no regressions? | ⚠️ | `test_microvm_client.py` is deleted without "
        "a replacement. |\n\n"
        "### Findings\n"
        "- 🟡 **Should fix** — `lma-virtual-participant-stack/test/"
        "test_microvm_client.py` — deleted with no replacement; the launch-mode "
        "assertions it carried are not covered elsewhere.\n"
        "- 🟢 **Nice to have** — `CHANGELOG.md` — no entry for a user-visible "
        "image bump.\n",
        id="ordinary-full-review",
    ),
]


@pytest.mark.parametrize("body", MUST_BE_PUBLISHABLE)
def test_disclosure_guard_clears_the_permitted_ordinary_senses(body: str) -> None:
    """CLAUDE.md's permitted senses, and the output shape the contract prescribes.

    A false positive withholds a legitimate review and loses its findings, and after
    it happens twice somebody deletes the guard. Keeping these green is what buys the
    guard the right to withhold anything at all.
    """
    tripped = review.scan_for_disclosure(body)
    assert not tripped, (
        f"a publishable review was withheld; rules that fired: {tripped}. CLAUDE.md "
        f"permits the ordinary senses of this vocabulary, so a rule matching one of "
        f"them needs a clearance pattern rather than a looser test."
    )


def test_the_footer_and_notice_are_themselves_publishable() -> None:
    """The harness's own composed text must pass its own guard.

    Not a tautology, and it has bitten before in spirit: the footer and the withheld
    notice are both part of the guard's input, and both are about disclosure, so both
    are the most likely text in the repository to trip it. If either did, EVERY
    comment would be withheld — including the notice explaining why — and the job
    would be silently useless.
    """
    merge_request = review.MergeRequest(
        iid=800,
        title="feat(config): restrict configuration operations to the Admin group",
        author="someone",
        source_branch="feature/x",
        target_branch="develop",
        web_url="https://github.com/o/r/pull/800",
        head_sha="0" * 40,
        draft=False,
    )

    note = review.compose_note(
        merge_request,
        "## PR/MR Review: a title (#800)\n\nLooks fine.",
        (3, 40, 2),
        False,
        review.DEFAULT_MODEL,
    )
    assert not review.scan_for_disclosure(note), (
        "compose_note's own footer trips the guard, so no review could ever be posted"
    )

    notice = review.withheld_notice(
        merge_request, ["attack-narrative/attacker"], review.DEFAULT_MODEL
    )
    assert not review.scan_for_disclosure(notice), (
        "the withheld notice trips the guard, so a withheld review could not even "
        "publish the explanation of why it was withheld"
    )


def test_the_guard_reports_rule_names_and_never_the_matched_text() -> None:
    """The return value is published (to a public job log), so it must carry no excerpt.

    ``scan_for_disclosure`` returns names from ``DISCLOSURE_RULES`` and the caller
    prints them into the job log, which on a public repository is downloadable by
    anyone with an account. So the invariant asserted here is the strong form — the
    return value is a subset of a FIXED set of names that exists before any input is
    read — because that, and not a word check, is what makes it impossible for the
    scanned text to reach the log.
    """
    known = {rule.name for rule in review.DISCLOSURE_RULES}
    for secret in (
        "an attacker can reach the invented handler without a session",
        "any authenticated user can rewrite the invented record",
        "this is a stored XSS in the invented panel",
    ):
        tripped = review.scan_for_disclosure(secret)
        assert tripped, "the guard did not fire, so this assertion proves nothing"
        assert set(tripped) <= known, (
            f"the guard returned {set(tripped) - known}, which is not a rule name — so "
            f"something derived from the scanned text reaches the public job log"
        )
        # Words unique to the input, which no static rule name can contain.
        for word in ("invented", "handler", "session", "record", "panel"):
            assert not any(word in name for name in tripped)


def test_every_rule_name_is_unique_and_generic() -> None:
    """Rule names are published in the notice and the log, so they describe no code here.

    They name the SHAPE ("attack-narrative/forgery"), which is a statement about
    English, not about this repository. Uniqueness matters because the name is the
    only handle an author has on which part of the contract was missed.
    """
    names = [rule.name for rule in review.DISCLOSURE_RULES]
    assert len(names) == len(set(names)), f"duplicate rule name in {names}"
    for name in names:
        assert re.fullmatch(r"[a-z]+(?:-[a-z]+)*/[a-z]+(?:-[a-z]+)*", name), (
            f"{name!r} does not follow the category/shape convention"
        )


def test_identifier_masking_does_not_swallow_prose_in_backticks() -> None:
    """A sentence does not become publishable by being quoted.

    Identifier-shaped spans are masked so a path or a test name can carry a listed
    word. Prose in backticks is NOT masked, because the easiest way to defeat the
    guard would otherwise be to wrap the finding in a code span — and a review that
    quotes an attack narrative has published it just the same.
    """
    assert review.scan_for_disclosure("`an attacker can read the store`")
    assert not review.scan_for_disclosure("`test_unauthenticated_user_rejected`")


# --- 3. The four workflow properties most likely to be deleted ---------------


@pytest.fixture(scope="module")
def gh_workflow() -> dict:
    return yaml.safe_load(GITHUB_WORKFLOW.read_text())


@pytest.fixture(scope="module")
def gh_job(gh_workflow: dict) -> dict:
    return gh_workflow["jobs"]["ai_pr_review"]


def test_the_cost_controls_are_still_in_place(gh_workflow: dict, gh_job: dict) -> None:
    """`cancel-in-progress`, a per-PR concurrency group, and the draft exclusion.

    A review is a real Bedrock spend and the job triggers automatically, so these are
    what stand between a push burst and paying for a review per push.
    `cancel-in-progress` is the main one and the easiest to remove by accident; the
    group has to be per pull request or a scheduled sweep and a PR's own review cancel
    each other.
    """
    concurrency = gh_workflow.get("concurrency") or {}
    assert concurrency.get("cancel-in-progress") is True, (
        "cancel-in-progress is off, so a burst of pushes pays for a full review each "
        "instead of the newer run cancelling the older one"
    )
    assert "pull_request.number" in str(concurrency.get("group", "")), (
        "the concurrency group must be per pull request, or a scheduled sweep and a "
        "pull request's own review cancel each other"
    )

    condition = str(gh_job.get("if", ""))
    assert "draft == false" in condition, (
        "the job no longer excludes drafts, so the WIP phase — the part of a pull "
        "request's life with the most pushes — now pays for a review each time"
    )

    runs = "\n".join(str(step.get("run", "")) for step in gh_job["steps"] if "run" in step)
    assert "--max-mrs 3" in runs, (
        "the sweep's bound is gone. At --timeout 1200 a batch of ten has a 200-minute "
        "worst case against this job's 45m limit, so the job would be cut off "
        "mid-review rather than the sweep bounding itself."
    )


def test_the_harness_is_pinned_to_the_target_branch(gh_job: dict) -> None:
    """The step without which a pull request reviews itself with its own reviewer.

    On a `pull_request` run the checkout contains the PR's changes, so the permission
    mode, both tool lists, the secret-stripping list, the disclosure guard and the
    prompt would all be author-controlled. The step must also HARD FAIL when the path
    list does not resolve: a soft warning there means the one case the control cannot
    work is also the case nobody is told about.
    """
    steps = [s for s in gh_job["steps"] if "git checkout FETCH_HEAD" in str(s.get("run", ""))]
    assert steps, (
        "no step checks the harness out of the target branch, so on a pull request "
        "the reviewer is the pull request's own copy of the reviewer"
    )
    body = str(steps[0]["run"])
    for path in (
        "scripts/pr_review/ai_pr_review.py",
        ".claude/skills/pr-review.md",
        ".claude/skills/pr-review-ci.md",
        "CLAUDE.md",
    ):
        assert path in body, f"{path} is no longer pinned to the target branch"
    assert "exit 1" in body, (
        "the pinning step no longer fails when the path list does not resolve, so a "
        "renamed file would silently fall back to running the pull request's own copy"
    )


def test_the_checkout_does_not_persist_the_job_token(gh_job: dict) -> None:
    """`persist-credentials: false`, because this job hands a model a checkout.

    actions/checkout otherwise leaves the job's token in `.git/config` as an
    http.extraheader. The model gets no Bash and no network tool so it could not USE
    the token — but it can read files and its output is published, so a persisted
    credential is an exfiltration channel that costs one line to close.
    """
    checkouts = [s for s in gh_job["steps"] if "actions/checkout" in str(s.get("uses", ""))]
    assert checkouts, "the job no longer checks out the repository"
    for step in checkouts:
        assert (step.get("with") or {}).get("persist-credentials") is False, (
            "persist-credentials is not false, so the job's token is written into "
            ".git/config in a checkout that is then handed to a model"
        )


def test_the_job_requests_only_the_permissions_it_needs(gh_job: dict) -> None:
    """Three scopes, each with a specific job, and `contents` stays read.

    `pull-requests: write` posts the comment and `id-token: write` mints the OIDC
    token for the Bedrock role. `contents: write` would let a review that went wrong
    push to the repository, and nothing here needs it.
    """
    permissions = gh_job.get("permissions") or {}
    assert permissions.get("contents") == "read"
    assert permissions.get("pull-requests") == "write"
    assert permissions.get("id-token") == "write"
    assert set(permissions) == {"contents", "pull-requests", "id-token"}, (
        f"the job's permissions grew beyond the three it needs: {permissions}"
    )


# --- 4. Advisory, on both platforms ------------------------------------------


def test_nothing_in_the_tree_claims_this_job_as_a_required_check() -> None:
    """A model's opinion must not gate a merge, and skipping is why that is subtle.

    GitHub reports a conditionally skipped job as SUCCEEDING, and this job skips on
    drafts and on fork pull requests — so a required context would pass on exactly the
    pull requests no review ran for. Branch protection itself lives in repository
    settings and cannot be asserted from here; what can be asserted is that nothing
    committed to the tree (a ruleset file, another workflow's `needs:`, a reusable
    workflow) names it as something to wait for or require.
    """
    job_name = yaml.safe_load(GITHUB_WORKFLOW.read_text())["jobs"]["ai_pr_review"]["name"]
    handles = {"ai_pr_review", job_name}

    # Structural claims only, not prose: another workflow is free to MENTION this one
    # in a comment (code-checks.yml does, where it runs the review's unit tests). What
    # it must not do is depend on it or declare it required.
    claimants: list[str] = []
    for path in sorted((REPO_ROOT / ".github").rglob("*.y*ml")):
        try:
            document = yaml.safe_load(path.read_text())
        except yaml.YAMLError:  # pragma: no cover - a malformed file is another test
            continue
        if not isinstance(document, dict):
            continue
        relative = str(path.relative_to(REPO_ROOT))

        for job_id, job in (document.get("jobs") or {}).items():
            if not isinstance(job, dict) or (path == GITHUB_WORKFLOW and job_id == "ai_pr_review"):
                continue
            needs = job.get("needs")
            needs = [needs] if isinstance(needs, str) else list(needs or [])
            if handles & set(needs):
                claimants.append(f"{relative}:jobs.{job_id}.needs")

        # A ruleset or branch-protection document committed to the tree. None exists
        # today, which is exactly why this looks for the shape rather than a filename:
        # the first one added must not quietly list this context.
        for key in ("required_status_checks", "required_checks", "requiredStatusChecks"):
            found = json_like_strings(document.get(key))
            if handles & found:
                claimants.append(f"{relative}:{key}")

    assert not claimants, (
        f"{claimants} depend on or require the advisory review job. Requiring a "
        f"conditionally skipped job means the gate passes on every draft and every "
        f"fork pull request with no review having run, because GitHub reports a "
        f"skipped job as succeeding."
    )


def json_like_strings(node: object) -> set[str]:
    """Every string anywhere inside a nested mapping/sequence.

    Used by the required-check test, because the shape of a ruleset document is not
    fixed — a context can sit under a list of strings or a list of
    ``{context: ..., integration_id: ...}`` mappings — and a test that assumed one
    shape would pass while the other listed this job.
    """
    if isinstance(node, str):
        return {node}
    if isinstance(node, dict):
        return set().union(*(json_like_strings(value) for value in node.values())) or set()
    if isinstance(node, (list, tuple)):
        return set().union(*(json_like_strings(item) for item in node)) or set()
    return set()


def test_the_gitlab_job_is_explicitly_allowed_to_fail() -> None:
    """GitLab's spelling of advisory. Without it a review failure reddens the pipeline."""
    config = yaml.safe_load(GITLAB_CI.read_text())
    job = config["ai_mr_review"]
    assert job.get("allow_failure") is True, (
        "ai_mr_review is no longer allow_failure, so a model's opinion — or a Bedrock "
        "outage — now fails the pipeline"
    )
    assert job.get("interruptible") is True, (
        "interruptible is off, which is GitLab's half of the push-burst cost bound"
    )


# --- 5. Pins, and the two configurations agreeing ----------------------------


def test_every_action_is_pinned_to_a_commit_sha(gh_job: dict) -> None:
    """A floating action tag on a job holding a credential is a supply-chain hole.

    It is also the convention code-checks.yml already follows, kept current by the
    `github-actions` entry in .github/dependabot.yml.
    """
    for step in gh_job["steps"]:
        uses = str(step.get("uses", ""))
        if not uses:
            continue
        action, _, ref = uses.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"{action} is pinned to {ref!r}, not a 40-character commit SHA"
        )


def test_the_cli_version_and_model_match_across_both_ci_configurations() -> None:
    """Two platforms running the same reviewer at different versions is a silent skew.

    It would only ever show up as one of them producing an oddly different review, so
    it is asserted rather than left to whoever edits one file.
    """
    workflow_text = GITHUB_WORKFLOW.read_text()
    gitlab_text = GITLAB_CI.read_text()

    cli = re.search(r"CLAUDE_CODE_VERSION:\s*['\"]([^'\"]+)['\"]", workflow_text)
    assert cli, "the workflow does not pin CLAUDE_CODE_VERSION"
    assert re.search(rf"CLAUDE_CODE_VERSION:\s*['\"]{re.escape(cli.group(1))}['\"]", gitlab_text), (
        f"the GitHub workflow pins Claude Code {cli.group(1)} but .gitlab-ci.yml pins "
        f"a different version"
    )

    model = re.search(r"ANTHROPIC_MODEL:\s*['\"]([^'\"]+)['\"]", workflow_text)
    assert model, "the workflow does not pin ANTHROPIC_MODEL"
    assert re.search(rf"ANTHROPIC_MODEL:\s*['\"]{re.escape(model.group(1))}['\"]", gitlab_text), (
        "the two CI configurations name different Bedrock models"
    )
    assert model.group(1) == review.DEFAULT_MODEL, (
        f"the workflow pins {model.group(1)} but the harness defaults to "
        f"{review.DEFAULT_MODEL}; a local run and a CI run would use different models"
    )
    # The IAM policy is scoped to a model family, so a model change that forgets the
    # template produces an AccessDenied at invoke time rather than a lint failure.
    family = re.search(r"ModelIdPrefix:.*?Default:\s*(\S+)", ROLE_TEMPLATE.read_text(), re.S)
    assert family, "the role template no longer has a ModelIdPrefix default"
    assert family.group(1) in model.group(1), (
        f"the role template grants bedrock:InvokeModel on {family.group(1)}* but the "
        f"job runs {model.group(1)}; the credential step would succeed and the invoke "
        f"would fail with AccessDenied"
    )


def test_both_jobs_state_the_forge_on_the_command_line() -> None:
    """Every invocation must pass --forge, because the harness does not sniff.

    An invocation that omits it does not fail — it defaults to GitHub, reads
    `$GITHUB_TOKEN`, finds nothing on the GitLab runner, and reports a loud skip about
    a variable nobody set, on a job that is allowed to be advisory and so goes
    unnoticed.
    """
    pattern = re.compile(r"python3\s+scripts/pr_review/ai_pr_review\.py(?:[^\n]*\\\n)*[^\n]*")
    for path, expected in ((GITHUB_WORKFLOW, "--forge github"), (GITLAB_CI, "--forge gitlab")):
        # Matched on the INVOCATION, not the filename: the harness-pinning step names
        # the same path among the files it pins, so counting mentions of the path would
        # score that step as an invocation that forgot the flag.
        invocations = pattern.findall(path.read_text())
        assert invocations, f"{path.name} never invokes the reviewer"
        for invocation in invocations:
            assert expected in invocation, (
                f"an invocation in {path.name} does not pass `{expected}`: {invocation!r}"
            )


# --- 6. The constraints that are only true if written down -------------------


def test_the_schedule_and_its_default_branch_constraint_are_documented(
    gh_workflow: dict,
) -> None:
    """The fork path, and the fact that `on: schedule` is read from the default branch.

    Both halves are asserted because both are invisible. A fork's `pull_request` run
    gets no OIDC token, so the schedule is the only path a fork pull request has. And
    GitHub reads `on: schedule` from the DEFAULT branch's copy of the workflow — which
    for this repository IS `develop`, so the sweep works on merge; if the default
    branch is ever changed, the cron stops firing silently and the role template's
    DefaultBranch parameter has to change with it. Nothing but the header states that.
    """
    # `on:` parses to the YAML 1.1 boolean True, which is why it is read both ways.
    triggers = gh_workflow.get(True, gh_workflow.get("on"))
    assert isinstance(triggers, dict), f"the workflow's `on:` is not a mapping: {triggers!r}"
    assert "schedule" in triggers, (
        "the schedule is gone, so fork pull requests can no longer be reviewed at all "
        "— their own runs get no OIDC token and so cannot reach Bedrock"
    )
    assert "workflow_dispatch" in triggers, (
        "workflow_dispatch is gone, so a fork pull request cannot be reviewed on "
        "demand and must wait for the next scheduled tick"
    )
    text = GITHUB_WORKFLOW.read_text()
    assert "DEFAULT BRANCH" in text, (
        "the workflow no longer records that `on: schedule` is read from the default "
        "branch. That constraint is the difference between a sweep that runs and one "
        "that silently never fires, and nothing else states it."
    )


def test_the_ci_skill_carries_the_disclosure_contract() -> None:
    """The output shape the guard enforces must be WRITTEN somewhere the model reads.

    A guard with no contract is a guard that withholds every security finding: the
    model is never told the shape, writes the natural one, and the review is dropped.
    So the skill file has to carry both the required elements and the escape hatch for
    a finding that cannot be stated at all.
    """
    text = CI_SKILL.read_text()
    for required in (
        "the file, the\nline, and the control that is needed",
        "CONTRIBUTING.md",
        "scan_for_disclosure",
        "the whole review is withheld",
    ):
        assert required in text.replace("\r\n", "\n"), (
            f"pr-review-ci.md no longer states {required!r}, so the model is not told "
            f"the output shape the guard enforces"
        )
