# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the scheduled pipeline's own machinery. No AWS, no stack.

Everything else in this directory needs a deployed stack, so it runs only when
someone asks for it. These do not, and they run in the fast pipeline
(`make test-integ-plumbing`), because they cover the two pieces that decide
whether a scheduled run can report success without having tested anything:

  * ``cognito_test_user.resolve`` — where the opt-in audio test's credentials
    come from. If it silently returns nothing, that test skips.
  * the ``--no-skips`` hook in conftest.py — which turns such a skip into a
    failure, and is therefore the thing standing between a lapsed secret and a
    green nightly.
  * ``junit_summary`` — the chat notification's body. It runs in the job's
    ``after_script``, after a run that may have failed in any way, so what it
    has to do is produce a payload in every one of those cases rather than a
    traceback where the notification should have been.

The job's AWS credentials are not covered here: they are vended by the runner
rather than acquired by code in this repository, so the thing that can go wrong
is the role's trust policy, and the pipeline is what exercises it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cognito_test_user  # noqa: E402
import junit_summary  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_credential_cache():
    """resolve() memoizes, which would leak one test's answer into the next."""
    cognito_test_user._resolve_cached.cache_clear()
    yield
    cognito_test_user._resolve_cached.cache_clear()


# ── where the test user comes from ──────────────────────────────────────────


def test_environment_credentials_are_used_when_both_are_set(monkeypatch):
    monkeypatch.setenv("LMA_TEST_USERNAME", "someone@example.com")
    monkeypatch.setenv("LMA_TEST_PASSWORD", "from-the-environment")
    monkeypatch.delenv("LMA_TEST_USER_SECRET_ID", raising=False)
    assert cognito_test_user.resolve() == ("someone@example.com", "from-the-environment")


def test_no_credentials_configured_resolves_to_none(monkeypatch):
    """The local default: nothing set, so the opt-in test skips rather than errors."""
    for name in ("LMA_TEST_USERNAME", "LMA_TEST_PASSWORD", "LMA_TEST_USER_SECRET_ID"):
        monkeypatch.delenv(name, raising=False)
    assert cognito_test_user.resolve() is None
    assert cognito_test_user.available() is False


def test_a_username_without_a_password_is_not_treated_as_configured(monkeypatch):
    """Half-set environment variables must not look like a usable credential."""
    monkeypatch.setenv("LMA_TEST_USERNAME", "someone@example.com")
    monkeypatch.delenv("LMA_TEST_PASSWORD", raising=False)
    monkeypatch.delenv("LMA_TEST_USER_SECRET_ID", raising=False)
    assert cognito_test_user.resolve() is None


def _stub_secret(monkeypatch, payload: object) -> mock.Mock:
    """Point the secret path at a fake Secrets Manager returning ``payload``.

    Returns the stub client, so a test can assert on how it was called.
    """
    monkeypatch.delenv("LMA_TEST_USERNAME", raising=False)
    monkeypatch.delenv("LMA_TEST_PASSWORD", raising=False)
    monkeypatch.setenv("LMA_TEST_USER_SECRET_ID", "lma/integ/test-user")
    client = mock.Mock()
    body = payload if isinstance(payload, str) else json.dumps(payload)
    client.get_secret_value.return_value = {"SecretString": body}
    session = mock.Mock()
    session.client.return_value = client
    monkeypatch.setattr("boto3.Session", mock.Mock(return_value=session))
    return client


def test_secret_manager_credentials_are_used_when_no_environment_variables(monkeypatch):
    client = _stub_secret(
        monkeypatch, {"username": "ci@example.com", "password": "from-the-secret"}
    )
    assert cognito_test_user.resolve("us-west-2") == ("ci@example.com", "from-the-secret")
    client.get_secret_value.assert_called_once_with(SecretId="lma/integ/test-user")


def test_email_is_accepted_as_the_username_key(monkeypatch):
    """A Cognito pool configured for email sign-in calls the username 'email'."""
    _stub_secret(monkeypatch, {"email": "ci@example.com", "password": "pw"})
    assert cognito_test_user.resolve() == ("ci@example.com", "pw")


def test_environment_variables_take_precedence_over_the_secret(monkeypatch):
    _stub_secret(monkeypatch, {"username": "secret@example.com", "password": "secret-pw"})
    monkeypatch.setenv("LMA_TEST_USERNAME", "env@example.com")
    monkeypatch.setenv("LMA_TEST_PASSWORD", "env-pw")
    assert cognito_test_user.resolve() == ("env@example.com", "env-pw")


def test_secret_missing_the_password_key_raises_rather_than_skipping(monkeypatch):
    """A malformed secret must fail loudly; returning None would skip the test."""
    _stub_secret(monkeypatch, {"username": "ci@example.com"})
    with pytest.raises(cognito_test_user.CredentialsUnavailable, match="has no password"):
        cognito_test_user.resolve()


def test_secret_that_is_not_json_raises_rather_than_skipping(monkeypatch):
    _stub_secret(monkeypatch, "not-json-at-all")
    with pytest.raises(cognito_test_user.CredentialsUnavailable, match="is not JSON"):
        cognito_test_user.resolve()


def test_unreadable_secret_raises_rather_than_skipping(monkeypatch):
    """A misspelled secret id, or a role without permission to read it."""
    monkeypatch.delenv("LMA_TEST_USERNAME", raising=False)
    monkeypatch.delenv("LMA_TEST_PASSWORD", raising=False)
    monkeypatch.setenv("LMA_TEST_USER_SECRET_ID", "lma/integ/does-not-exist")
    client = mock.Mock()
    client.get_secret_value.side_effect = RuntimeError("ResourceNotFoundException")
    session = mock.Mock()
    session.client.return_value = client
    monkeypatch.setattr("boto3.Session", mock.Mock(return_value=session))
    with pytest.raises(cognito_test_user.CredentialsUnavailable, match="could not read"):
        cognito_test_user.resolve()


def test_available_reports_false_for_a_broken_source_without_raising(monkeypatch):
    """`available()` is the skip guard, so it must not raise out of a test body."""
    _stub_secret(monkeypatch, {"username": "ci@example.com"})
    assert cognito_test_user.available() is False


# ── the notification payload built from the JUnit report ────────────────────

_PASSING_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="0" skipped="0" tests="3" time="412.5">
    <testcase classname="t" name="test_stack_status_is_complete" time="1.0"/>
    <testcase classname="t" name="test_appsync_reachable" time="2.0"/>
    <testcase classname="t" name="test_ws_stream_transcribes_to_meeting" time="409.5"/>
  </testsuite>
</testsuites>
"""

_FAILING_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="1" skipped="0" tests="4" time="300.0">
    <testcase classname="t" name="test_stack_status_is_complete" time="1.0"/>
    <testcase classname="t" name="test_appsync_reachable" time="2.0"/>
    <testcase classname="t" name="test_ws_stream_transcribes_to_meeting" time="200.0">
      <failure message="AssertionError: no transcript segments were produced within 90s">
      Traceback (most recent call last):
        File "integ-tests/test_lma_integration.py", line 400, in test_ws_stream
      AssertionError: no transcript segments were produced within 90s
      </failure>
    </testcase>
    <testcase classname="t" name="test_vp_registry_lifecycle" time="97.0">
      <error message="botocore.exceptions.ClientError: AccessDenied"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "integ-tests-report.xml"
    path.write_text(body, encoding="utf-8")
    return path


def test_counts_and_duration_are_read_from_the_report(tmp_path: Path):
    report = junit_summary.parse_report(_write(tmp_path, _PASSING_REPORT))
    assert report.problem is None
    assert (report.total, report.passed, report.failures, report.skipped) == (3, 3, 0, 0)
    assert report.duration == pytest.approx(412.5)


def test_failing_tests_are_named_with_their_assertion_in_the_summary(tmp_path: Path):
    report = junit_summary.parse_report(_write(tmp_path, _FAILING_REPORT))
    summary = junit_summary.build_summary(
        report, stack="lma-integtest1", region="us-west-2", ok=False
    )
    assert "lma-integtest1 (us-west-2)" in summary
    assert "2 passed, 2 failed" in summary
    assert "FAILED test_ws_stream_transcribes_to_meeting" in summary
    assert "no transcript segments were produced" in summary
    # An <error> is a different element from a <failure> and must not be dropped.
    assert "ERROR test_vp_registry_lifecycle" in summary


def test_passing_runs_do_not_list_per_test_detail(tmp_path: Path):
    report = junit_summary.parse_report(_write(tmp_path, _PASSING_REPORT))
    summary = junit_summary.build_summary(report, stack="lma-integtest1", ok=True)
    assert "3 passed, 0 failed" in summary
    assert "FAILED" not in summary


def test_a_missing_report_is_summarised_rather_than_raising(tmp_path: Path):
    """The job can die in before_script, before pytest writes anything."""
    report = junit_summary.parse_report(tmp_path / "absent.xml")
    assert report.problem is not None
    summary = junit_summary.build_summary(report, stack="lma-integtest1", ok=False)
    assert "no results to report" in summary
    assert "No JUnit report" in summary


def test_an_unparseable_report_is_summarised_rather_than_raising(tmp_path: Path):
    """A job killed by its timeout can leave a half-written report."""
    report = junit_summary.parse_report(_write(tmp_path, "<testsuites><testsuite tests="))
    assert report.problem is not None
    assert "Could not parse" in junit_summary.build_summary(report, ok=False)


def test_a_run_that_collected_no_tests_says_so(tmp_path: Path):
    """Zero tests with a green job would otherwise read as a passing nightly."""
    empty = '<testsuites><testsuite name="pytest" tests="0" failures="0" errors="0" '
    empty += 'skipped="0" time="0.1"/></testsuites>'
    report = junit_summary.parse_report(_write(tmp_path, empty))
    assert "No tests ran" in junit_summary.build_summary(report, ok=True)


def test_skips_are_listed_when_they_are_all_there_is(tmp_path: Path):
    """--no-skips should prevent this, so seeing it means that hook regressed."""
    body = """<testsuites>
      <testsuite name="pytest" errors="0" failures="0" skipped="1" tests="1" time="1.0">
        <testcase classname="t" name="test_ws_stream_transcribes_to_meeting">
          <skipped message="no Cognito test user configured"/>
        </testcase>
      </testsuite>
    </testsuites>"""
    report = junit_summary.parse_report(_write(tmp_path, body))
    summary = junit_summary.build_summary(report, ok=True)
    assert "SKIPPED test_ws_stream_transcribes_to_meeting" in summary
    assert "no Cognito test user configured" in summary


def test_summary_is_capped_so_slack_accepts_the_message(tmp_path: Path):
    """Slack rejects a message over its limit outright rather than truncating."""
    cases = "".join(
        f'<testcase classname="t" name="test_{i}"><failure message="{"x" * 500}"/></testcase>'
        for i in range(40)
    )
    body = (
        '<testsuites><testsuite name="pytest" errors="0" failures="40" skipped="0" '
        f'tests="40" time="10.0">{cases}</testsuite></testsuites>'
    )
    report = junit_summary.parse_report(_write(tmp_path, body))
    summary = junit_summary.build_summary(report, ok=False, max_chars=2000)
    assert len(summary) <= 2000 + 60  # the cap plus the "truncated" marker
    assert "truncated" in summary


def test_payload_keys_match_the_workflow_builder_contract(tmp_path: Path):
    """A Workflow Builder trigger silently ignores keys it does not declare."""
    report = junit_summary.parse_report(_write(tmp_path, _PASSING_REPORT))
    payload = junit_summary.build_payload(
        report,
        status="success",
        job_url="https://example.com/jobs/1",
        commit="0123456789abcdef",
        stack="lma-integtest1",
    )
    assert set(payload) == {"status", "commit", "job_url", "summary"}
    # Flat strings only: a Text variable takes nothing else.
    assert all(isinstance(value, str) for value in payload.values())
    assert payload["status"] == "✅ PASSED"
    assert payload["commit"] == "01234567"
    # Bare URL, not <url|label>: Workflow Builder shows that syntax literally.
    assert payload["job_url"] == "https://example.com/jobs/1"


@pytest.mark.parametrize(
    ("job_status", "expected"),
    [("success", "✅ PASSED"), ("failed", "❌ FAILED"), ("canceled", "⚠️ CANCELED")],
)
def test_job_status_decides_the_headline(tmp_path: Path, job_status: str, expected: str):
    report = junit_summary.parse_report(_write(tmp_path, _PASSING_REPORT))
    payload = junit_summary.build_payload(
        report, status=job_status, job_url="", commit="abc", stack="s"
    )
    assert payload["status"] == expected


def test_a_green_job_with_failures_in_its_report_is_flagged(tmp_path: Path):
    """The two disagreeing is itself worth saying, rather than posting green."""
    report = junit_summary.parse_report(_write(tmp_path, _FAILING_REPORT))
    payload = junit_summary.build_payload(
        report, status="success", job_url="", commit="abc", stack="s"
    )
    assert "report lists failures" in payload["status"]


def test_the_cli_prints_a_json_payload(tmp_path: Path, capsys):
    path = _write(tmp_path, _PASSING_REPORT)
    exit_code = junit_summary.main(
        [
            "--report",
            str(path),
            "--status",
            "success",
            "--job-url",
            "https://example.com/jobs/1",
            "--commit",
            "0123456789abcdef",
            "--stack",
            "lma-integtest1",
            "--region",
            "us-west-2",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    # stdout is the payload and nothing else: the job pipes it into a file curl
    # posts verbatim, so a stray print there would corrupt the request body.
    payload = json.loads(captured.out)
    assert payload["status"] == "✅ PASSED"
    assert "lma-integtest1 (us-west-2)" in payload["summary"]
    # The readable form goes to stderr, which is what the job log shows.
    assert "✅ PASSED" in captured.err
    assert "lma-integtest1 (us-west-2)" in captured.err


def test_the_cli_succeeds_when_there_is_no_report(tmp_path: Path, capsys):
    """after_script must still get a payload out of a job that died early."""
    exit_code = junit_summary.main(["--report", str(tmp_path / "absent.xml"), "--status", "failed"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "❌ FAILED"
    assert "No JUnit report" in payload["summary"]


def test_a_failed_publish_stage_names_the_stage_rather_than_the_missing_report(
    tmp_path: Path, capsys
):
    """The publish job has no report to write, and that is not the news."""
    exit_code = junit_summary.main(
        [
            "--report",
            str(tmp_path / "absent.xml"),
            "--status",
            "failed",
            "--stage",
            "publish",
            "--stack",
            "lma-integtest1",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "❌ FAILED"
    assert "publish stage failed" in payload["summary"]
    assert "no tests ran" in payload["summary"]
    # Says the stack is untouched, which is the thing worth knowing at 8pm.
    assert "unchanged" in payload["summary"]
    assert "No JUnit report" not in payload["summary"]


def test_a_failed_deploy_stage_warns_that_the_stack_may_need_attention(tmp_path: Path, capsys):
    """A stack stuck mid-update fails every following night, so say so once."""
    exit_code = junit_summary.main(
        [
            "--report",
            str(tmp_path / "absent.xml"),
            "--status",
            "failed",
            "--stage",
            "deploy",
            "--stack",
            "lma-integtest1",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "deploy stage failed" in payload["summary"]
    assert "every night" in payload["summary"]


def test_the_tests_stage_is_the_default_and_still_summarises_the_report(tmp_path: Path, capsys):
    """Passing no --stage must not change what the tests job has always posted."""
    report = _write(tmp_path, _PASSING_REPORT)
    exit_code = junit_summary.main(
        ["--report", str(report), "--status", "success", "--stack", "lma-integtest1"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "✅ PASSED"
    assert "3 passed" in payload["summary"]
    assert "stage failed" not in payload["summary"]


def test_an_unknown_stage_is_refused_rather_than_silently_mislabelled(tmp_path: Path):
    """A typo in the job definition must fail loudly, not post a wrong label."""
    with pytest.raises(SystemExit):
        junit_summary.main(
            ["--report", str(tmp_path / "absent.xml"), "--status", "failed", "--stage", "deployy"]
        )


# ── the per-test listing ────────────────────────────────────────────────────

_GROUPED_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="5" time="100.0">
  <testcase classname="integ-tests.test_lma_integration" name="test_stack_status_is_complete" time="1.0"/>
  <testcase classname="integ-tests.test_lma_integration" name="test_output_present[A]" time="0.1"/>
  <testcase classname="integ-tests.test_lma_integration" name="test_output_present[B]" time="0.1"/>
  <testcase classname="integ-tests.test_lma_integration" name="test_appsync_reachable" time="2.0"/>
  <testcase classname="integ-tests.test_lma_integration" name="test_ws_stream_transcribes_to_meeting" time="90.0">
    <failure message="AssertionError: no transcript segments">trace</failure>
  </testcase>
</testsuite></testsuites>
"""


def test_every_test_is_named_in_the_summary(tmp_path: Path, capsys):
    """The counts say how it went; the list says what it covered."""
    report = _write(tmp_path, _GROUPED_REPORT)
    junit_summary.main(["--report", str(report), "--status", "failed", "--stack", "s"])
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert "stack_status_is_complete ✓" in summary
    assert "appsync_reachable ✓" in summary
    assert "ws_stream_transcribes_to_meeting ✗" in summary


def test_parametrised_cases_collapse_to_one_line_with_a_count(tmp_path: Path, capsys):
    """Seven values of one check are one check, not seven lines of a capped message."""
    report = _write(tmp_path, _GROUPED_REPORT)
    junit_summary.main(["--report", str(report), "--status", "failed", "--stack", "s"])
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert "output_present ✓ (×2)" in summary
    assert "output_present[A]" not in summary


def test_a_module_with_too_many_checks_is_summarised_not_listed(tmp_path: Path, capsys):
    """The plumbing suite is numerous and uniform; it must not bury the live suite."""
    cases = "".join(
        f'<testcase classname="integ-tests.test_ci_plumbing" name="test_p{i}" time="0.01"/>'
        for i in range(30)
    )
    report = _write(
        tmp_path,
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
        f'errors="0" failures="0" skipped="0" tests="30" time="1.0">{cases}</testsuite></testsuites>',
    )
    junit_summary.main(["--report", str(report), "--status", "success", "--stack", "s"])
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert "test_ci_plumbing — all 30 passed" in summary
    assert "p17" not in summary


def test_the_deployed_template_is_named_when_given(tmp_path: Path, capsys):
    report = _write(tmp_path, _PASSING_REPORT)
    junit_summary.main(
        [
            "--report",
            str(report),
            "--status",
            "success",
            "--template-url",
            "https://s3.us-west-2.amazonaws.com/bucket/nightly/lma-main.yaml",
        ]
    )
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert "Deployed: https://s3.us-west-2.amazonaws.com/bucket/nightly/lma-main.yaml" in summary


# ── the triage agent's report, as the notification sees it ──────────────────


def test_the_agent_report_is_appended_under_its_own_heading(tmp_path: Path, capsys):
    report = _write(tmp_path, _FAILING_REPORT)
    agent = tmp_path / "triage-report.txt"
    agent.write_text("ROOT CAUSE: the transcriber rejected the upgrade\nEVIDENCE: a log line\n")
    junit_summary.main(
        ["--report", str(report), "--status", "failed", "--agent-report", str(agent)]
    )
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert "🤖 Triage:" in summary
    assert "ROOT CAUSE: the transcriber rejected the upgrade" in summary
    # The deterministic detail is still there: the agent adds to it, never
    # replaces it.
    assert "test_ws_stream_transcribes_to_meeting" in summary


def test_an_empty_or_absent_agent_report_changes_nothing(tmp_path: Path, capsys):
    """A green run touches the file; a disabled agent writes nothing to it."""
    report = _write(tmp_path, _PASSING_REPORT)
    empty = tmp_path / "triage-report.txt"
    empty.write_text("")
    junit_summary.main(
        ["--report", str(report), "--status", "success", "--agent-report", str(empty)]
    )
    with_empty = json.loads(capsys.readouterr().out)["summary"]

    junit_summary.main(
        ["--report", str(report), "--status", "success", "--agent-report", str(tmp_path / "nope")]
    )
    with_absent = json.loads(capsys.readouterr().out)["summary"]

    assert "Triage" not in with_empty
    assert with_empty == with_absent


def test_a_verbose_agent_cannot_crowd_out_the_test_results(tmp_path: Path, capsys):
    """The facts outrank the interpretation, so both get their own budget."""
    report = _write(tmp_path, _FAILING_REPORT)
    agent = tmp_path / "triage-report.txt"
    agent.write_text("ROOT CAUSE: x\n" + "padding line\n" * 2000)
    junit_summary.main(
        ["--report", str(report), "--status", "failed", "--agent-report", str(agent)]
    )
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert "test_ws_stream_transcribes_to_meeting" in summary
    assert "ROOT CAUSE: x" in summary
    # Slack rejects the whole post past 4000 characters.
    assert len(summary) < 4000


def test_the_agent_refuses_to_run_without_read_only_credentials(monkeypatch):
    """It would otherwise inherit the nightly's administrative credentials."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import failure_agent  # noqa: PLC0415

    monkeypatch.setattr(failure_agent, "AGENT_ENABLED", True)
    monkeypatch.setattr(failure_agent, "AGENT_ROLE_ARN", "")
    # Would raise if reached: nothing may be launched without the role.
    monkeypatch.setattr(
        failure_agent, "ensure_claude_code", lambda: pytest.fail("launched without a role")
    )
    assert (
        failure_agent.run_failure_agent(
            report_path=Path("x.xml"), log_path=Path("x.log"), stack="s", region="us-west-2"
        )
        is None
    )


def test_the_agent_is_off_unless_explicitly_enabled(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import failure_agent  # noqa: PLC0415

    monkeypatch.setattr(failure_agent, "AGENT_ENABLED", False)
    monkeypatch.setattr(
        failure_agent, "_readonly_credentials", lambda: pytest.fail("ran while disabled")
    )
    assert (
        failure_agent.run_failure_agent(
            report_path=Path("x.xml"), log_path=Path("x.log"), stack="s", region="us-west-2"
        )
        is None
    )
