# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Authentication, meeting access and every other entry point, on a deployed stack.

test_role_access.py holds each role to the role matrix at every operation. This
module adds what a role check alone does not establish:

* AppSync admits only a genuine, unexpired ID token of this user pool: every
  operation is refused with no credential, and a token whose claims or
  signature were changed, that is unsigned, signed by another key or naming
  another issuer, or that has expired, is refused;
* the operations only backend roles may call are refused to a request signed
  with the web UI's identity-pool credentials, for every role;
* a meeting is read by its owner, the users it is shared with and Admins, and
  by nobody else; it is changed, shared and deleted only as each operation's
  rule allows; its key frames' signed links go only to those readers; and the
  web UI's credentials read only the recordings prefixes;
* the WebSocket transcriber, the MCP server (API key endpoint and OAuth
  gateway), the Virtual Participant viewer path, the identity pool and the
  user pool's self sign-up refuse what they must; the stack declares no Lambda
  function URL and no REST API method without an authorizer.

Throwaway users (an Admin, three Users and a Participant), one throwaway
meeting and a temporary app client are created for the run and deleted
afterwards, also when a test fails. The web UI app client's ID token lifetime is
shortened to five minutes for one sign-in and restored at once. Every check is recorded in
scratch/api-test-results/report.json and report.md (security_report.py).
See docs/security-testing.md.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import jwt_tampering as jwt
import pytest
import role_access_probe as rbac
import security_access_probe as probe
import security_report
from security_report import REPORT

pytest.importorskip("pycognito")
pytest.importorskip("graphql")
pytest.importorskip("websockets")

ACCOUNTS = (
    ("sec-admin", "Admin"),
    ("sec-owner", "User"),
    ("sec-shared", "User"),
    ("sec-other", "User"),
    ("sec-participant", "Participant"),
)
READERS = ("sec-owner", "sec-shared", "sec-admin")


@pytest.fixture
def recorded():
    """Fails the test on any check it recorded that did not pass (checks of earlier tests are theirs)."""
    start = len(REPORT.checks)
    yield
    failed = [c for c in REPORT.checks[start:] if c.outcome == security_report.FAIL]
    assert not failed, security_report.describe_failures(failed)


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def deployment(client) -> rbac.Deployment:
    found = rbac.Deployment.find(client.stack_name, client.region)
    REPORT.stack = client.stack_name
    REPORT.region = found.session.region_name or ""
    return found


@pytest.fixture(scope="module")
def accounts(deployment: rbac.Deployment):
    probe.sweep_stale_accounts(deployment)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    made = rbac.create_role_users(deployment, stamp, ACCOUNTS)
    try:
        for user in made:
            rbac.sign_in(deployment, user)
        yield {label: user for (label, _), user in zip(ACCOUNTS, made, strict=True)}
    finally:
        left = rbac.delete_role_users(deployment, made)
        assert not left, f"could not delete the temporary users {left}"


@pytest.fixture(scope="module")
def short_lived(deployment: rbac.Deployment, accounts):
    """A genuine web UI ID token that expires five minutes after it is issued, and its expiry.

    Issued at the first test that asks for it, so the wait for its expiry
    overlaps the tests in between. The web UI app client's ID token lifetime is
    shortened for that one sign-in and restored at once (see
    sign_in_with_short_id_token); the restore is a check of its own.
    """
    tokens, restored, error = probe.sign_in_with_short_id_token(deployment, accounts["sec-other"])
    REPORT.record(
        probe.SUITE_AUTH,
        "web UI app client settings restored after the short-token sign-in",
        "-",
        "restored",
        "restored" if restored else "not restored",
        error,
    )
    assert restored, f"the web UI app client's settings could not be restored: {error}"
    yield tokens, jwt.split(tokens["id"])[1]["exp"]


@pytest.fixture(scope="module")
def meeting(deployment: rbac.Deployment, accounts):
    """The throwaway meeting: owned by sec-owner, shared by them with sec-shared, seeded."""
    created = probe.Meeting(deployment, accounts["sec-owner"])
    try:
        created.create()
        created.seed()
        shared_user = accounts["sec-shared"].username
        status, body = probe.as_user(
            deployment, created.owner, probe.SHARE, {"calls": created.calls_input(), "to": shared_user}
        )
        applied = probe.wait_until(lambda: shared_user in created.shared_with(), probe.SHARE_WAIT_SECONDS)
        REPORT.record(
            probe.SUITE_MEETING,
            "shareMeetings (share the meeting)",
            "sec-owner",
            "applied",
            "applied" if applied else "not applied",
            "" if applied else f"HTTP {status} {probe.error_text(body)}",
        )
        if not applied:
            raise probe.ProbeError("the owner could not share the throwaway meeting; see report.md")
        # Wait for the sharing to reach the meeting's lines, which the share writes after the meeting.
        probe.wait_until(
            lambda: created.segment_id
            in probe.segment_ids(
                probe.as_user(deployment, accounts["sec-shared"], probe.GET_SEGMENTS, {"id": created.call_id})[1],
                "getTranscriptSegments",
            ),
            probe.SHARE_WAIT_SECONDS,
        )
        yield created
    finally:
        created.cleanup()


# ── AppSync authentication ─────────────────────────────────────────────────


def _operations() -> list[str]:
    roles = rbac.matrix_roles(rbac.load_matrix())
    return sorted(op for op in roles if not op.startswith("Subscription."))


def test_appsync_refuses_every_operation_without_a_credential(deployment, recorded) -> None:
    schema = rbac.load_schema()
    for operation in _operations():
        document, variables = rbac.build_operation(schema, operation)
        status, body = probe.graphql_raw(deployment.graphql_url, {}, document, variables)
        REPORT.record(
            probe.SUITE_AUTH,
            f"{operation} with no Authorization header",
            "unauthenticated",
            "refused",
            "refused" if probe.refused_at_request(status, body) else "answered",
            f"HTTP {status} {probe.error_text(body)}",
        )


def test_appsync_refuses_altered_unsigned_and_foreign_id_tokens(deployment, accounts, short_lived, recorded) -> None:
    schema = rbac.load_schema()
    genuine = accounts["sec-other"].tokens["id"]
    claims = jwt.split(genuine)[1]
    variants = {
        "a genuine User ID token (control)": genuine,
        "the Admin group added to its claims": jwt.with_claims(genuine, **{"cognito:groups": ["Admin"]}),
        "its expiry moved a day later": jwt.with_claims(genuine, exp=claims["exp"] + 86400),
        "its signature altered": jwt.with_altered_signature(genuine),
        "an alg none header and no signature": jwt.unsigned(genuine),
        "its claims signed by another key": jwt.self_signed(genuine),
        "signed by another key, naming another user pool": jwt.self_signed(
            genuine, issuer=jwt.other_issuer(claims["iss"]), **{"cognito:groups": ["Admin"]}
        ),
        "the Authorization header holding no token": "Bearer ",
    }
    operations = ("Query.getCallCount", "Query.listUsers", "Mutation.deleteMeetings")
    for label, token in variants.items():
        for operation in operations:
            document, variables = rbac.build_operation(schema, operation)
            status, body = probe.graphql_raw(deployment.graphql_url, {"Authorization": token}, document, variables)
            observed = probe.outcome_of(status, body)
            expected = "refused"
            if label.endswith("(control)"):
                # A User is admitted to the call count and refused the Admin-only
                # listUsers at the field; deleteMeetings answers a placeholder.
                observed = probe.outcome_of(status, body, operation)
                expected = "refused" if operation == "Query.listUsers" else "answered"
            REPORT.record(
                probe.SUITE_AUTH,
                f"{operation} with {label}",
                "sec-other (User)",
                expected,
                observed,
                f"HTTP {status} {probe.error_text(body)}",
            )
    # The short-lived token, while it is fresh: the control for the expiry check
    # at the end of this module.
    fresh, _ = short_lived
    status, body = probe.graphql_raw(deployment.graphql_url, {"Authorization": fresh["id"]}, probe.PROBE_QUERY)
    REPORT.record(
        probe.SUITE_AUTH,
        "Query.getCallCount with a fresh five-minute ID token (control)",
        "sec-other (User)",
        "answered",
        probe.outcome_of(status, body, "Query.getCallCount"),
        f"HTTP {status} {probe.error_text(body)}",
    )
    # A genuine token of the same user, issued to another app client of the pool.
    with probe.TemporaryAppClient(deployment) as other_client:
        foreign = other_client.sign_in(accounts["sec-other"])
    status, body = probe.graphql_raw(deployment.graphql_url, {"Authorization": foreign["id"]}, probe.PROBE_QUERY)
    REPORT.record(
        probe.SUITE_AUTH,
        "Query.getCallCount with a genuine ID token issued to another app client of the pool",
        "sec-other (User)",
        "refused",
        probe.outcome_of(status, body, "Query.getCallCount"),
        f"HTTP {status} {probe.error_text(body)}",
    )


# ── backend-only operations and the web UI's credentials ───────────────────


def test_iam_operations_refuse_the_web_ui_identity_pool_credentials(deployment, accounts, recorded) -> None:
    """The operations a backend role may call, signed with SigV4 by each role's identity-pool credentials.

    AppSync judges a SigV4 request by the caller's IAM permissions, not by the
    role matrix, so these must hold no appsync:GraphQL grant for the API.
    """
    matrix = rbac.load_matrix()
    schema = rbac.load_schema()
    # The mutations a signed-in role may also call are left out: some start real
    # work (a Virtual Participant, a model call) for a caller they admit.
    operations = sorted(
        [op for op in matrix["backendOnly"]["appsync"] if not op.startswith("Subscription.")]
        + [op for op in matrix["alsoIam"]["appsync"] if op.startswith("Query.")]
    )
    for label in ("sec-admin", "sec-owner", "sec-participant"):
        session = probe.identity_session(deployment, accounts[label])
        caller = f"{label} identity-pool credentials"
        if session is None:
            REPORT.record(probe.SUITE_BACKEND, "identity-pool credentials", caller, "no AppSync access", "no AppSync access", "the identity pool issued no credentials")
            continue
        for operation in operations:
            document, variables = rbac.build_operation(schema, operation)
            status, body = probe.sigv4_graphql(session, deployment.graphql_url, document, variables)
            REPORT.record(
                probe.SUITE_BACKEND,
                operation,
                caller,
                "refused",
                probe.outcome_of(status, body, operation),
                f"HTTP {status} {probe.error_text(body)}",
            )


# ── one meeting: owner, shared with, unrelated, Admin ──────────────────────


def _record_visibility(deployment, accounts, meeting, subject: str, document: str, visible) -> None:
    for label in (*READERS, "sec-other"):
        status, body = probe.as_user(deployment, accounts[label], document, {"id": meeting.call_id})
        REPORT.record(
            probe.SUITE_MEETING,
            subject,
            label,
            "visible" if label in READERS else "not visible",
            "visible" if visible(body) else "not visible",
            f"HTTP {status} {probe.error_text(body)}",
        )


def test_a_meeting_is_read_only_by_its_owner_the_users_it_is_shared_with_and_admins(deployment, accounts, meeting, recorded) -> None:
    call_id = meeting.call_id
    _record_visibility(
        deployment, accounts, meeting, "getCall", probe.GET_CALL,
        lambda b: ((b.get("data") or {}).get("getCall") or {}).get("CallId") == call_id,
    )
    for field_name, document in (
        ("getTranscriptSegments", probe.GET_SEGMENTS),
        ("getTranscriptSegmentsWithSentiment", probe.GET_SENTIMENT),
    ):
        _record_visibility(
            deployment, accounts, meeting, f"{field_name} (the seeded line)", document,
            lambda b, f=field_name: meeting.segment_id in probe.segment_ids(b, f),
        )
    _record_visibility(
        deployment, accounts, meeting, "getVideoKeyframes (the seeded key frame and its signed link)",
        probe.GET_KEYFRAMES,
        lambda b: any(f.get("FrameId") == meeting.frame_id and f.get("ImageSignedUrl") for f in probe.keyframes(b)),
    )
    for label in (*READERS, "sec-other"):
        listed, errors = probe.listed_call_ids(deployment, accounts[label], meeting)
        REPORT.record(
            probe.SUITE_MEETING,
            "listCallsDateRange (the meeting in the list)",
            label,
            "visible" if label in READERS else "not visible",
            "visible" if call_id in listed else "not visible",
            errors,
        )


def test_meeting_scoped_operations_refuse_a_user_who_cannot_see_the_meeting(deployment, accounts, meeting, recorded) -> None:
    """Each operation that names the meeting: answered to its owner, refused to an unrelated User.

    prepareMeetingArchive builds an archive for a caller it admits, so it is
    called only as the unrelated User.
    """
    cases = {
        "getChatAttachments": probe.GET_ATTACHMENTS,
        "getMeetingConsent": probe.GET_CONSENT,
        "getCallCost": probe.GET_COST,
        "listMeetingWhiteboards": probe.LIST_WHITEBOARDS,
        "listMeetingNotifications": probe.LIST_NOTIFICATIONS,
        "prepareMeetingArchive": probe.PREPARE_ARCHIVE,
    }
    for field_name, document in cases.items():
        callers = ("sec-other",) if field_name == "prepareMeetingArchive" else ("sec-owner", "sec-other")
        for label in callers:
            status, body = probe.as_user(deployment, accounts[label], document, {"id": meeting.call_id})
            refused = bool(body.get("errors")) and not (body.get("data") or {}).get(field_name)
            REPORT.record(
                probe.SUITE_MEETING,
                field_name,
                label,
                "refused" if label == "sec-other" else "answered",
                "refused" if refused else "answered",
                f"HTTP {status} {probe.error_text(body)}",
            )


def test_the_web_ui_credentials_read_only_the_recordings_prefixes(deployment, accounts, meeting, recorded) -> None:
    """The identity-pool grant: a User reads the audio and video recordings prefixes and no other; a Participant none.

    One object is written under each prefix for the run and deleted afterwards.
    """
    from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

    s3 = deployment.session.client("s3")
    keys = probe.recording_probe_keys(meeting.call_id)
    for key in keys.values():
        s3.put_object(Bucket=meeting.bucket, Key=key, Body=b"integ security test object")
    readable_prefixes = ("audio recording", "video recording")
    for label in ("sec-owner", "sec-participant"):
        session = probe.identity_session(deployment, accounts[label])
        for kind, key in keys.items():
            expected = "read" if label == "sec-owner" and kind in readable_prefixes else "refused"
            if session is None:
                REPORT.record(probe.SUITE_MEETING, f"S3 GetObject: {kind}", f"{label} identity-pool credentials", expected, "refused", "no credentials")
                continue
            try:
                session.client("s3").get_object(Bucket=meeting.bucket, Key=key)["Body"].read()
                observed, detail = "read", ""
            except ClientError as error:
                observed, detail = "refused", error.response.get("Error", {}).get("Code", "")
            REPORT.record(
                probe.SUITE_MEETING, f"S3 GetObject: {kind}", f"{label} identity-pool credentials",
                expected, observed, detail,
            )


# ── the other entry points ─────────────────────────────────────────────────


def test_the_transcriber_admits_only_a_genuine_access_token(deployment, accounts, recorded) -> None:
    access = accounts["sec-other"].tokens["access"]
    cases = {
        "no authorization parameter": (None, 401),
        "a value that is not a bearer token": ("garbage", 401),
        "a bearer value that is not a token": ("Bearer garbage", 401),
        "an ID token in place of the access token": (f"Bearer {accounts['sec-other'].tokens['id']}", 401),
        "an access token with an altered signature": (f"Bearer {jwt.with_altered_signature(access)}", 401),
        "an access token signed by another key": (f"Bearer {jwt.self_signed(access)}", 401),
        "a genuine User access token (control)": (f"Bearer {access}", 101),
    }
    for label, (authorization, expected) in cases.items():
        status = probe.transcriber_upgrade_status(deployment, authorization)
        REPORT.record(probe.SUITE_SURFACES, f"transcriber upgrade with {label}", "sec-other (User)", f"HTTP {expected}", f"HTTP {status}")


def test_the_transcriber_refuses_a_start_for_a_meeting_the_caller_does_not_own(deployment, accounts, meeting, recorded) -> None:
    """A START naming an existing meeting adds to it, so only its owner or an Admin may send one."""
    for label in ("sec-other", "sec-shared"):
        observed, detail = probe.transcriber_start_on(deployment, accounts[label], meeting.call_id)
        REPORT.record(probe.SUITE_SURFACES, "transcriber START naming another user's meeting", label, "refused", observed, detail)


def _mcp_endpoint(deployment, request) -> str:
    endpoint = deployment.outputs.get("MCPServerApiKeyEndpoint")
    if not endpoint:
        # A stack deployed with EnableMCP=false has nothing to check, so
        # --no-skips leaves this skip alone.
        request.node.add_marker(pytest.mark.optional_feature)
        pytest.skip("the stack has no MCP server (EnableMCP=false)")
    return endpoint


def test_the_mcp_api_key_endpoint_admits_only_a_current_key_and_follows_meeting_access(deployment, accounts, meeting, request, recorded) -> None:
    endpoint = _mcp_endpoint(deployment, request)
    for label, headers in (("no key", {}), ("a key that was never issued", {"x-api-key": "lma_00000000-0000-0000-0000-000000000000"})):
        status, _ = probe.jsonrpc(endpoint, headers, "tools/list")
        REPORT.record(probe.SUITE_SURFACES, f"MCP API key endpoint with {label}", "unauthenticated", "refused", "refused" if status in (401, 403) else f"HTTP {status}")
    transcript = {"name": "get_meeting_transcript", "arguments": {"meetingId": meeting.call_id, "format": "json"}}
    for label in ("sec-owner", "sec-shared", "sec-other"):
        with probe.McpKey(deployment, accounts[label]) as key:
            status, answer = probe.jsonrpc(endpoint, {"x-api-key": key.value}, "tools/call", transcript)
            ids, error = probe.mcp_transcript_ids(answer)
            REPORT.record(
                probe.SUITE_SURFACES, "MCP get_meeting_transcript (the seeded line)", label,
                "not visible" if label == "sec-other" else "visible",
                "visible" if meeting.segment_id in ids else "not visible", f"HTTP {status} {error}",
            )
            if label == "sec-other":
                key.revoke()
                status, _ = probe.jsonrpc(endpoint, {"x-api-key": key.value}, "tools/list")
                REPORT.record(probe.SUITE_SURFACES, "MCP API key endpoint with a revoked key", label, "refused", "refused" if status in (401, 403) else f"HTTP {status}")


def test_a_meeting_is_changed_shared_and_deleted_only_as_each_rule_allows(deployment, accounts, meeting, recorded) -> None:
    """Labels: owner and shared users may change them. Sharing and deleting: the owner only.

    The outcome is read from the stored meeting, not from the answer, so a
    refusal reported as success is caught. The owner's deletion comes last.
    """

    def label_write(label: str, value: str) -> None:
        before = meeting.labels()
        status, body = probe.as_user(
            deployment, accounts[label], probe.UPDATE_LABELS,
            {"id": meeting.call_id, "labels": [{"Key": "integ-sectest", "Value": value}]},
        )
        applied = probe.wait_until(lambda: value in meeting.labels(), 10, 2)
        REPORT.record(
            probe.SUITE_MEETING,
            "updateCallLabels",
            label,
            "not applied" if label == "sec-other" else "applied",
            "applied" if applied else "not applied",
            f"HTTP {status} {probe.error_text(body)}; labels before {before[:120]}",
        )

    label_write("sec-other", "written-by-other")
    label_write("sec-shared", "written-by-shared")
    label_write("sec-owner", "written-by-owner")

    for label in ("sec-shared", "sec-other"):
        recipient = accounts[label].username
        before = meeting.shared_with()
        status, body = probe.as_user(
            deployment, accounts[label], probe.SHARE,
            {"calls": meeting.calls_input(), "to": f"{recipient},{accounts['sec-other'].username}"},
        )
        time.sleep(3)
        after = meeting.shared_with()
        REPORT.record(
            probe.SUITE_MEETING,
            "shareMeetings",
            label,
            "not applied",
            "not applied" if after == before else "applied",
            f"HTTP {status} {probe.error_text(body)}",
        )

    for label in ("sec-shared", "sec-other"):
        status, body = probe.as_user(deployment, accounts[label], probe.DELETE, {"calls": meeting.calls_input()})
        time.sleep(5)
        REPORT.record(
            probe.SUITE_MEETING,
            "deleteMeetings",
            label,
            "not applied",
            "not applied" if meeting.row() else "applied",
            f"HTTP {status} {probe.error_text(body)}",
        )

    status, body = probe.as_user(deployment, accounts["sec-owner"], probe.DELETE, {"calls": meeting.calls_input()})
    deleted = probe.wait_until(lambda: meeting.row() is None, probe.DELETE_WAIT_SECONDS)
    REPORT.record(
        probe.SUITE_MEETING, "deleteMeetings", "sec-owner", "applied",
        "applied" if deleted else "not applied", f"HTTP {status} {probe.error_text(body)}",
    )
    if deleted:
        _, after = probe.as_user(deployment, accounts["sec-owner"], probe.GET_CALL, {"id": meeting.call_id})
        gone = not ((after.get("data") or {}).get("getCall") or {}).get("CallId")
        REPORT.record(
            probe.SUITE_MEETING, "getCall after the meeting was deleted", "sec-owner", "not visible",
            "not visible" if gone else "visible", probe.error_text(after),
        )


def test_the_mcp_oauth_gateway_admits_only_tokens_issued_to_its_app_client(deployment, accounts, request, recorded) -> None:
    _mcp_endpoint(deployment, request)
    gateway = deployment.outputs["MCPServerEndpoint"]
    tokens = accounts["sec-other"].tokens
    cases = {
        "no token": {},
        "a value that is not a token": {"Authorization": "Bearer garbage"},
        "a web UI ID token": {"Authorization": f"Bearer {tokens['id']}"},
        "a web UI access token": {"Authorization": f"Bearer {tokens['access']}"},
        "an access token signed by another key": {"Authorization": f"Bearer {jwt.self_signed(tokens['access'])}"},
    }
    for label, headers in cases.items():
        status, _ = probe.jsonrpc(gateway, headers, "tools/list")
        REPORT.record(probe.SUITE_SURFACES, f"MCP OAuth gateway with {label}", "sec-other (User)", "refused", "refused" if status in (401, 403) else f"HTTP {status}")


def test_the_virtual_participant_viewer_path_requires_a_signed_token(deployment, request, recorded) -> None:
    if "VNCALB" not in deployment.ai_resources:
        # Only the ECS launch types serve the viewer through CloudFront's /vnc/*.
        request.node.add_marker(pytest.mark.optional_feature)
        pytest.skip("the stack serves no /vnc/* path (Virtual Participant launch type MICROVM)")
    for label, token, expected in (("no token", None, 401), ("a token it did not sign", probe.forged_vnc_token(), 403)):
        status = probe.vnc_path_status(deployment, token)
        REPORT.record(probe.SUITE_SURFACES, f"/vnc/* with {label}", "unauthenticated", f"HTTP {expected}", f"HTTP {status}")


def test_the_identity_pool_and_user_pool_refuse_anonymous_and_outside_sign_ups(deployment, recorded) -> None:
    observed, detail = probe.unauthenticated_identity_refused(deployment)
    REPORT.record(probe.SUITE_SURFACES, "identity pool credentials with no login", "unauthenticated", "refused", observed, detail)
    parameters = deployment.session.client("cloudformation").describe_stacks(StackName=REPORT.stack)["Stacks"][0].get("Parameters", [])
    allowed = next((p.get("ParameterValue", "") for p in parameters if p["ParameterKey"] == "AllowedSignUpEmailDomain"), "")
    expected, observed, detail = probe.self_sign_up_outcome(deployment, [d.strip() for d in allowed.split(",") if d.strip()])
    REPORT.record(probe.SUITE_SURFACES, "self sign-up from a domain outside the allowed list", "unauthenticated", expected, observed, detail)


def test_the_stack_has_no_function_url_and_no_rest_method_without_an_authorizer(deployment, recorded) -> None:
    with_url = probe.function_urls(deployment, REPORT.stack)
    REPORT.record(probe.SUITE_SURFACES, "Lambda function URLs", "-", "none", "none" if not with_url else "present", ", ".join(with_url))
    checked, open_methods = probe.unauthorized_rest_methods(deployment, REPORT.stack)
    REPORT.record(
        probe.SUITE_SURFACES, f"REST API methods without an authorizer ({checked} checked)", "-", "none",
        "none" if not open_methods else "present", ", ".join(open_methods),
    )


# ── last, so the wait overlaps the tests above ─────────────────────────────


def test_appsync_refuses_a_genuine_id_token_after_it_expires(deployment, short_lived, recorded) -> None:
    tokens, expires_at = short_lived
    wait = expires_at + 20 - time.time()
    if wait > 0:
        time.sleep(wait)
    status, body = probe.graphql_raw(deployment.graphql_url, {"Authorization": tokens["id"]}, probe.PROBE_QUERY)
    REPORT.record(
        probe.SUITE_AUTH,
        "Query.getCallCount with an expired genuine ID token",
        "sec-other (User)",
        "refused",
        "refused" if probe.refused_at_request(status, body) else "answered",
        f"HTTP {status} {probe.error_text(body)} (waited {max(0, int(wait))} s)",
    )


def test_every_check_was_recorded_to_the_report() -> None:
    """The report the module promises: something was recorded, and the JSON form serialises."""
    assert REPORT.checks
    json.dumps(security_report.to_json(REPORT))
