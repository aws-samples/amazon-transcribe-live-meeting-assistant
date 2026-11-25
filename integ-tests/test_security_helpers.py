# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Offline tests of the live security tests' helpers: tokens, the report and the probe's pure functions.

No AWS and no stack: run by ``make test-integ-plumbing`` in CI, and by
``make security-results`` with the other static checks.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import jwt_tampering as jwt
import pytest
import security_report
from security_report import FAIL, NOT_RUN, PASS, Report

HERE = Path(__file__).resolve().parent
ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_AbCdEf123"


def _token(**claims) -> str:
    header = {"alg": "RS256", "kid": "pool-key-1"}
    body = {"sub": "s-1", "iss": ISSUER, "exp": 2_000_000_000, "cognito:groups": ["User"], **claims}
    segment = lambda value: jwt.b64url_encode(json.dumps(value).encode())  # noqa: E731
    return f"{segment(header)}.{segment(body)}.{jwt.b64url_encode(b'signature-bytes-0123456789')}"


# ── jwt_tampering ──────────────────────────────────────────────────────────


def test_split_reads_the_header_claims_and_signature() -> None:
    header, claims, signature = jwt.split(_token())
    assert header["kid"] == "pool-key-1"
    assert claims["iss"] == ISSUER
    assert jwt.b64url_decode(signature) == b"signature-bytes-0123456789"


def test_split_refuses_a_value_that_is_not_a_compact_jws() -> None:
    with pytest.raises(ValueError):
        jwt.split("only.two")


def test_with_claims_changes_the_claims_and_keeps_the_signature() -> None:
    token = _token()
    changed = jwt.with_claims(token, **{"cognito:groups": ["Admin"]})
    assert jwt.split(changed)[1]["cognito:groups"] == ["Admin"]
    assert jwt.split(changed)[1]["sub"] == "s-1"
    assert changed.split(".")[2] == token.split(".")[2]
    assert changed.split(".")[1] != token.split(".")[1]


def test_with_altered_signature_keeps_the_signed_part_and_changes_the_signature() -> None:
    token = _token()
    altered = jwt.with_altered_signature(token)
    assert altered.split(".")[:2] == token.split(".")[:2]
    original, changed = (jwt.b64url_decode(t.split(".")[2]) for t in (token, altered))
    assert len(original) == len(changed)
    assert sum(a != b for a, b in zip(original, changed, strict=True)) == 1


def test_unsigned_names_alg_none_and_carries_no_signature() -> None:
    header, claims, signature = jwt.split(jwt.unsigned(_token()))
    assert header["alg"] == "none"
    assert claims["sub"] == "s-1"
    assert signature == ""


def test_self_signed_uses_a_key_id_the_pool_does_not_publish_and_can_name_another_issuer() -> None:
    pytest.importorskip("cryptography")
    token = _token()
    signed = jwt.self_signed(token, issuer=jwt.other_issuer(ISSUER), extra="x")
    header, claims, signature = jwt.split(signed)
    assert header["alg"] == "RS256"
    assert header["kid"].startswith("integ-") and header["kid"] != "pool-key-1"
    assert claims["iss"] != ISSUER and claims["iss"].startswith("https://cognito-idp.us-west-2.amazonaws.com/")
    assert claims["extra"] == "x" and claims["sub"] == "s-1"
    assert len(jwt.b64url_decode(signature)) == 256  # a 2048-bit RSA signature
    assert jwt.split(jwt.self_signed(token))[1]["iss"] == ISSUER


def test_other_issuer_names_a_pool_in_the_same_region() -> None:
    assert jwt.other_issuer(ISSUER) == "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_IntegNoSuchPool"


# ── security_report ────────────────────────────────────────────────────────


def _report() -> Report:
    report = Report(stack="LMA-test", region="us-west-2")
    report.record("A", "Query.x with no token", "unauthenticated", "refused", "refused")
    report.record("A", "Query.y with no token", "unauthenticated", "refused", "answered", "HTTP 200 | data")
    report.record("B", "getCall", "sec-other", "not visible", "not visible")
    report.not_run("B", "/vnc/*", "unauthenticated", "HTTP 401", "no viewer path on this stack")
    return report


def test_a_check_passes_only_when_observed_equals_expected() -> None:
    report = _report()
    assert [c.outcome for c in report.checks] == [PASS, FAIL, PASS, NOT_RUN]
    assert report.counts() == {"A": {PASS: 1, FAIL: 1, NOT_RUN: 0}, "B": {PASS: 1, FAIL: 0, NOT_RUN: 1}}
    assert report.totals() == {PASS: 2, FAIL: 1, NOT_RUN: 1}
    assert [c.subject for c in report.failures("A")] == ["Query.y with no token"]
    assert report.failures("B") == []


def test_the_markdown_report_lists_counts_then_only_the_checks_that_did_not_pass() -> None:
    text = security_report.to_markdown(_report())
    assert "2 passed, 1 failed, 1 not run." in text
    assert "| A | 1 | 1 | 0 |" in text
    assert "## Failed checks" in text and "Query.y with no token" in text
    assert "HTTP 200 \\| data" in text  # a pipe in a cell is escaped
    assert "## Checks not run" in text and "no viewer path" in text
    assert "Query.x with no token" not in text


def test_the_json_report_holds_every_check(tmp_path: Path) -> None:
    written = security_report.write(_report(), tmp_path)
    assert written is not None
    data = json.loads(written[0].read_text())
    assert data["stack"] == "LMA-test" and data["totals"][FAIL] == 1
    assert len(data["checks"]) == 4 and data["checks"][1]["detail"] == "HTTP 200 | data"
    assert written[1].read_text().startswith("# LMA live access checks")


def test_nothing_is_written_when_nothing_was_recorded(tmp_path: Path) -> None:
    assert security_report.write(Report(), tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_the_failure_message_is_bounded(monkeypatch) -> None:
    report = Report()
    for n in range(50):
        report.record("A", f"op{n}", "r", "refused", "answered")
    monkeypatch.delenv("CI", raising=False)
    message = security_report.describe_failures(report.failures(), limit=5)
    assert message.count("\n") == 5 and "45 more" in message


def test_in_ci_the_failure_message_gives_counts_only(monkeypatch) -> None:
    report = Report()
    report.record("A", "Query.secretOperation", "sec-other", "refused", "answered", "HTTP 200 detail")
    report.record("B", "getCall", "sec-other", "not visible", "visible")
    monkeypatch.setenv("CI", "true")
    message = security_report.describe_failures(report.failures())
    assert message.startswith("2 access checks failed (A: 1, B: 1)")
    assert "secretOperation" not in message and "detail" not in message and "getCall" not in message
    monkeypatch.delenv("CI")
    assert "Query.secretOperation" in security_report.describe_failures(report.failures())


def test_the_default_report_directory_is_ignored_by_git() -> None:
    assert security_report.DEFAULT_DIR.relative_to(security_report.REPO_ROOT).parts[0] == "scratch"
    ignored = (security_report.REPO_ROOT / ".gitignore").read_text().split()
    assert "scratch" in ignored or "scratch/" in ignored


# ── security_access_probe (pure parts) ─────────────────────────────────────


def _probe():
    return pytest.importorskip("security_access_probe")


def test_a_request_refusal_is_a_401_or_403_with_no_data() -> None:
    probe = _probe()
    refusal = {"errors": [{"errorType": "UnauthorizedException", "message": "Valid authorization header not provided."}]}
    assert probe.refused_at_request(401, refusal)
    assert probe.refused_at_request(403, {})
    assert not probe.refused_at_request(200, refusal)
    assert not probe.refused_at_request(401, {"data": None, "errors": []})
    assert probe.outcome_of(401, refusal) == "refused"
    field_refusal = {
        "data": {"listUsers": None},
        "errors": [{"errorType": "Unauthorized", "message": "Not Authorized to access listUsers on type Query"}],
    }
    assert probe.outcome_of(200, field_refusal) == "answered"
    assert probe.outcome_of(200, field_refusal, "Query.listUsers") == "refused"
    assert probe.error_text(field_refusal).startswith("Unauthorized: Not Authorized")


def test_the_short_token_settings_change_only_the_id_token_lifetime() -> None:
    probe = _probe()
    described = {
        "UserPoolId": "us-west-2_x",
        "ClientId": "abc",
        "ClientName": "web",
        "IdTokenValidity": 1,
        "AccessTokenValidity": 1,
        "RefreshTokenValidity": 30,
        "TokenValidityUnits": {"AccessToken": "hours"},
        "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH"],
        "CallbackURLs": ["https://example.invalid/"],
        "CreationDate": "not a setting",
    }
    settings = probe.client_settings(described)
    assert "UserPoolId" not in settings and "CreationDate" not in settings and "ClientId" not in settings
    assert settings["CallbackURLs"] == ["https://example.invalid/"]
    short = probe.shortened_settings(settings)
    assert short["IdTokenValidity"] == probe.SHORT_TOKEN_MINUTES
    assert short["TokenValidityUnits"] == {"AccessToken": "hours", "IdToken": "minutes", "RefreshToken": "days"}
    assert {k: v for k, v in short.items() if k not in ("IdTokenValidity", "TokenValidityUnits")} == {
        k: v for k, v in settings.items() if k not in ("IdTokenValidity", "TokenValidityUnits")
    }
    assert settings["TokenValidityUnits"] == {"AccessToken": "hours"}  # the original is not changed


def test_the_restore_names_every_token_unit_so_the_shortened_unit_does_not_stay() -> None:
    """A client created with no units reports none; restoring without them would keep "minutes"."""
    probe = _probe()
    template_client = {"ClientName": "web", "IdTokenValidity": 1, "AccessTokenValidity": 1, "RefreshTokenValidity": 30}
    restore = probe.effective_settings(probe.client_settings(template_client))
    assert restore["TokenValidityUnits"] == {"AccessToken": "hours", "IdToken": "hours", "RefreshToken": "days"}
    assert restore["IdTokenValidity"] == 1
    # What the client reports after the restore compares equal to what was read before.
    units = {"IdToken": "hours", "AccessToken": "hours", "RefreshToken": "days"}
    after = {**template_client, "TokenValidityUnits": units}
    assert probe.effective_settings(probe.client_settings(after)) == restore


def test_every_setting_update_user_pool_client_accepts_is_restored() -> None:
    """UpdateUserPoolClient resets a setting it is not given, so the list must name every one."""
    botocore = pytest.importorskip("botocore.session")
    model = botocore.get_session().get_service_model("cognito-idp")
    accepted = set(model.operation_model("UpdateUserPoolClient").input_shape.members) - {"UserPoolId", "ClientId"}
    assert accepted <= set(_probe().CLIENT_SETTINGS), sorted(accepted - set(_probe().CLIENT_SETTINGS))


def test_the_probe_objects_sit_under_the_bucket_prefixes_the_access_doc_names() -> None:
    keys = _probe().recording_probe_keys("call-1")
    assert {k: v.split("/")[0] for k, v in keys.items()} == {
        "audio recording": "lma-audio-recordings",
        "video recording": "lma-video-recordings",
        "transcript file": "lma-transcripts",
        "pending upload": "lma-uploads-pending",
    }
    doc = (HERE.parent / "docs" / "user-based-access-control.md").read_text()
    for prefix in {v.split("/")[0] for v in keys.values()}:
        assert f"`{prefix}/`" in doc


def test_an_mcp_transcript_answer_is_read_for_its_segment_ids() -> None:
    probe = _probe()
    answer = {"result": {"content": [{"text": json.dumps({"segments": [{"segmentId": "s1"}, {"segmentId": "s2"}]})}]}}
    assert probe.mcp_transcript_ids(answer) == (["s1", "s2"], "")
    refused = {"result": {"isError": True, "content": [{"text": "Meeting not found"}]}}
    assert probe.mcp_transcript_ids(refused) == ([], "Meeting not found")
    assert probe.mcp_transcript_ids({"message": "Forbidden"})[0] == []


def test_segment_and_key_frame_answers_are_read_defensively() -> None:
    probe = _probe()
    body = {"data": {"getTranscriptSegments": {"TranscriptSegments": [{"SegmentId": "a"}, None]}}}
    assert probe.segment_ids(body, "getTranscriptSegments") == ["a"]
    assert probe.segment_ids({"data": {"getTranscriptSegments": None}}, "getTranscriptSegments") == []
    assert probe.keyframes({"data": None}) == []
    sentiment = {"data": {"x": {"TranscriptSegmentsWithSentiment": [{"SegmentId": "b"}]}}}
    assert probe.segment_ids(sentiment, "x") == ["b"]


def test_the_seeded_key_frame_sits_in_the_folder_the_resolver_signs() -> None:
    """The key-frame resolver signs only keys in the meeting's folder, named by posixify_filename."""
    probe = _probe()
    source = (HERE.parent / "lma-ai-stack/source/lambda_functions/video_keyframes_resolver/index.py").read_text()
    namespace: dict = {}
    exec(  # noqa: S102 - the resolver's own helper, compiled from the repository
        "import re\n" + source[source.index("def posixify_filename") : source.index("def key_in_folder")], namespace
    )
    for call_id in ("integ-sectest-20261124T232958-0ead6374", "-a b-", "x.y_z"):
        assert probe.posixify(call_id) == namespace["posixify_filename"](call_id)


def test_the_forged_viewer_token_has_the_shape_of_a_real_one() -> None:
    probe = _probe()
    payload, signature = probe.forged_vnc_token().split(".")
    assert set(json.loads(jwt.b64url_decode(payload))) == {"vpId", "prefix", "exp"}
    assert signature


# ── the test module itself ─────────────────────────────────────────────────


def test_the_live_module_makes_one_account_of_each_kind_it_needs() -> None:
    tree = ast.parse((HERE / "test_security_access.py").read_text())
    accounts = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "ACCOUNTS"
    )
    assert [role for _, role in accounts] == ["Admin", "User", "User", "User", "Participant"]
    assert all(label.startswith("sec-") for label, _ in accounts)


def test_every_live_test_fails_on_its_own_recorded_checks() -> None:
    """Each test asks for the ``recorded`` fixture, which fails it on any check it recorded that did not pass."""
    tree = ast.parse((HERE / "test_security_access.py").read_text())
    tests = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert len(tests) > 10
    missing = [
        t.name
        for t in tests
        if "recorded" not in {a.arg for a in t.args.args} and t.name != "test_every_check_was_recorded_to_the_report"
    ]
    assert not missing


def test_the_expiry_check_is_the_last_live_test_that_calls_the_stack() -> None:
    """The five-minute wait overlaps every test before it only if it comes last."""
    tree = ast.parse((HERE / "test_security_access.py").read_text())
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert names[-2:] == ["test_appsync_refuses_a_genuine_id_token_after_it_expires", "test_every_check_was_recorded_to_the_report"]


def test_the_meeting_is_deleted_only_after_every_test_that_reads_it() -> None:
    tree = ast.parse((HERE / "test_security_access.py").read_text())
    tests = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    users = [t.name for t in tests if "meeting" in {a.arg for a in t.args.args}]
    assert users[-1] == "test_a_meeting_is_changed_shared_and_deleted_only_as_each_rule_allows"


def test_the_client_settings_are_kept_beside_the_report_while_changed(monkeypatch, tmp_path: Path) -> None:
    probe = _probe()
    monkeypatch.setenv("LMA_SECURITY_REPORT_DIR", str(tmp_path))
    assert probe.settings_record_path() == tmp_path / "web-client-settings-before-change.json"
    monkeypatch.delenv("LMA_SECURITY_REPORT_DIR")
    assert probe.settings_record_path().parent == security_report.DEFAULT_DIR
