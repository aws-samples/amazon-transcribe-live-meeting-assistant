# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Live helpers for test_security_access.py: authentication, meeting access and the other entry points.

Builds on role_access_probe.py (the role matrix, the throwaway users and the
GraphQL call builder) and adds what that module does not call:

* AppSync with no credential, with altered and self-made ID tokens
  (jwt_tampering.py), with a genuine token issued to another app client of the
  pool (a temporary one, deleted afterwards), and with a genuine ID token after
  it has expired (the web UI app client's ID token lifetime is shortened to
  five minutes for one sign-in and restored at once);
* AppSync signed with SigV4 by the web UI's identity-pool credentials, for the
  operations only backend roles may call;
* one throwaway meeting, owned by one throwaway User, shared with a second and
  invisible to a third, with a transcript segment and a key frame, read,
  changed, shared and deleted as each of them;
* the WebSocket transcriber, the MCP server's API key endpoint and OAuth
  gateway, the Virtual Participant viewer's CloudFront path, the identity pool
  and the user pool's self sign-up, and the stack's Lambda functions and REST
  API methods.

Safety: every user is created for the run with a random password and deleted
afterwards; mutations that a caller is allowed to make are made only on the
throwaway meeting, every other mutation names an id that does not exist; the
app client change is undone, and the temporary app client and every key and
object the run makes are deleted, in a ``finally``; and users and app clients
left by an interrupted run are swept by their prefix the next time.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import role_access_probe as rbac

SUITE_AUTH = "AppSync authentication"
SUITE_ROLES = "AppSync role matrix"
SUITE_BACKEND = "AppSync backend-only operations"
SUITE_MEETING = "Meeting access"
SUITE_SURFACES = "Other entry points"

USER_PREFIX = "integ-rbac-"
CLIENT_PREFIX = "integ-sectest-"
SHORT_TOKEN_MINUTES = 5
STALE_AFTER = timedelta(hours=2)

# The one query every signed-in role but Participant may call and that reads nothing.
PROBE_QUERY = "query SecurityProbe { getCallCount { __typename } }"


# ── raw HTTP and GraphQL ───────────────────────────────────────────────────


def http(method: str, url: str, headers: dict | None = None, body: bytes | None = None, timeout: float = 30):
    """(status, response headers, body bytes); never raises for an HTTP status."""
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - https stack endpoint
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


def graphql_raw(url: str, headers: dict, document: str, variables: dict | None = None) -> tuple[int, dict]:
    status, _, raw = http(
        "POST",
        url,
        {"Content-Type": "application/json", **headers},
        json.dumps({"query": document, "variables": variables or {}}).encode(),
    )
    try:
        body = json.loads(raw or b"null")
    except json.JSONDecodeError:
        body = None
    return status, body if isinstance(body, dict) else {}


def sigv4_graphql(session, url: str, document: str, variables: dict | None = None) -> tuple[int, dict]:
    """A GraphQL call signed with the session's credentials, as the backend roles sign them."""
    from urllib.parse import urlparse  # pylint: disable=import-outside-toplevel

    from botocore.auth import SigV4Auth  # pylint: disable=import-outside-toplevel
    from botocore.awsrequest import AWSRequest  # pylint: disable=import-outside-toplevel

    data = json.dumps({"query": document, "variables": variables or {}}).encode()
    host = urlparse(url).hostname or ""
    region = host.split(".")[2] if host.count(".") >= 3 else session.region_name
    request = AWSRequest(method="POST", url=url, data=data, headers={"Content-Type": "application/json", "Host": host})
    SigV4Auth(session.get_credentials().get_frozen_credentials(), "appsync", region).add_auth(request)
    status, _, raw = http("POST", url, dict(request.headers), data)
    try:
        body = json.loads(raw or b"null")
    except json.JSONDecodeError:
        body = None
    return status, body if isinstance(body, dict) else {}


def error_types(body: dict) -> list[str]:
    return [str(e.get("errorType") or "") for e in (body or {}).get("errors") or []]


def error_text(body: dict) -> str:
    return "; ".join(f"{e.get('errorType')}: {str(e.get('message'))[:120]}" for e in (body or {}).get("errors") or [])


def refused_at_request(status: int, body: dict) -> bool:
    """Whether AppSync refused the request before reading the operation (no data at all)."""
    return status in (401, 403) and "data" not in (body or {})


def outcome_of(status: int, body: dict, operation: str | None = None) -> str:
    """``refused`` when AppSync refused the request or the field; else ``answered``."""
    if refused_at_request(status, body):
        return "refused"
    if operation and rbac.refused_at_field(body, operation):
        return "refused"
    return "answered"


# ── accounts ───────────────────────────────────────────────────────────────


def sweep_stale_accounts(deployment: rbac.Deployment, now: datetime | None = None) -> list[str]:
    """Delete users and app clients an interrupted run left, by their prefix, once two hours old."""
    cognito = deployment.session.client("cognito-idp")
    cutoff = (now or datetime.now(timezone.utc)) - STALE_AFTER
    removed: list[str] = []
    for page in cognito.get_paginator("list_users").paginate(
        UserPoolId=deployment.pool_id, Filter=f'email ^= "{USER_PREFIX}"'
    ):
        for user in page.get("Users", []):
            if user["UserCreateDate"] < cutoff and user["Username"].startswith(USER_PREFIX):
                cognito.admin_delete_user(UserPoolId=deployment.pool_id, Username=user["Username"])
                removed.append(user["Username"])
    for page in cognito.get_paginator("list_user_pool_clients").paginate(UserPoolId=deployment.pool_id):
        for client in page.get("UserPoolClients", []):
            if not client["ClientName"].startswith(CLIENT_PREFIX):
                continue
            described = cognito.describe_user_pool_client(UserPoolId=deployment.pool_id, ClientId=client["ClientId"])
            if described["UserPoolClient"]["CreationDate"] < cutoff:
                cognito.delete_user_pool_client(UserPoolId=deployment.pool_id, ClientId=client["ClientId"])
                removed.append(client["ClientName"])
    return removed


@dataclass
class TemporaryAppClient:
    """A temporary app client of the user pool, deleted when the block ends."""

    deployment: rbac.Deployment
    client_id: str = ""
    name: str = ""

    def __enter__(self) -> "TemporaryAppClient":
        cognito = self.deployment.session.client("cognito-idp")
        self.name = f"{CLIENT_PREFIX}{datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%S')}"
        created = cognito.create_user_pool_client(
            UserPoolId=self.deployment.pool_id,
            ClientName=self.name,
            GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            IdTokenValidity=SHORT_TOKEN_MINUTES,
            AccessTokenValidity=SHORT_TOKEN_MINUTES,
            RefreshTokenValidity=60,
            TokenValidityUnits={"IdToken": "minutes", "AccessToken": "minutes", "RefreshToken": "minutes"},
            PreventUserExistenceErrors="ENABLED",
        )
        self.client_id = created["UserPoolClient"]["ClientId"]
        return self

    def sign_in(self, user: rbac.RoleUser) -> dict:
        from ws_stream_probe import _mint_tokens  # pylint: disable=import-outside-toplevel

        return _mint_tokens(
            self.deployment.session.region_name, self.deployment.pool_id, self.client_id, user.username, user.password
        )

    def __exit__(self, *_exc) -> None:
        if self.client_id:
            self.deployment.session.client("cognito-idp").delete_user_pool_client(
                UserPoolId=self.deployment.pool_id, ClientId=self.client_id
            )


# The settings UpdateUserPoolClient accepts: one it is not given is reset to its
# default, so every one the client has is passed back.
CLIENT_SETTINGS = (
    "ClientName",
    "RefreshTokenValidity",
    "AccessTokenValidity",
    "IdTokenValidity",
    "TokenValidityUnits",
    "ReadAttributes",
    "WriteAttributes",
    "ExplicitAuthFlows",
    "SupportedIdentityProviders",
    "CallbackURLs",
    "LogoutURLs",
    "DefaultRedirectURI",
    "AllowedOAuthFlows",
    "AllowedOAuthScopes",
    "AllowedOAuthFlowsUserPoolClient",
    "AnalyticsConfiguration",
    "PreventUserExistenceErrors",
    "EnableTokenRevocation",
    "EnablePropagateAdditionalUserContextData",
    "AuthSessionValidity",
    "RefreshTokenRotation",
    "PublishSecretsInAWSSecretsManager",
)


# Cognito's units for a client that names none. A client created without
# TokenValidityUnits (as the stack's template creates the web UI's) reports none,
# and an update that omits them keeps the units the client has, so a restore must
# name them: after the shortening they are minutes, which would turn the
# original lifetime of 1 (hour) into 1 minute, below Cognito's minimum.
DEFAULT_TOKEN_UNITS = {"AccessToken": "hours", "IdToken": "hours", "RefreshToken": "days"}


def client_settings(described: dict) -> dict:
    return {name: described[name] for name in CLIENT_SETTINGS if name in described}


def effective_settings(settings: dict) -> dict:
    """The settings with every token unit named, the defaults filled in: what restores them exactly."""
    return {**settings, "TokenValidityUnits": {**DEFAULT_TOKEN_UNITS, **(settings.get("TokenValidityUnits") or {})}}


def shortened_settings(settings: dict, minutes: int = SHORT_TOKEN_MINUTES) -> dict:
    """The client's settings with the ID token lifetime set to ``minutes`` and every other setting kept."""
    effective = effective_settings(settings)
    units = {**effective["TokenValidityUnits"], "IdToken": "minutes"}
    return {**effective, "IdTokenValidity": minutes, "TokenValidityUnits": units}


def sign_in_with_short_id_token(deployment: rbac.Deployment, user: rbac.RoleUser) -> tuple[dict, bool, str]:
    """Web UI tokens whose ID token lasts five minutes; whether the client was restored, and the last error.

    AppSync accepts tokens of the web UI's app client only, so the expiry check
    needs one of its tokens, and its ID tokens last an hour. The lifetime is
    shortened for this one sign-in, a few seconds, and every setting of the
    client is put back in a ``finally``, with retries, before the tokens are
    used. A token issued meanwhile to anyone else lasts five minutes and is
    renewed by the web UI as usual.
    """
    from ws_stream_probe import _mint_tokens  # pylint: disable=import-outside-toplevel

    cognito = deployment.session.client("cognito-idp")
    described = cognito.describe_user_pool_client(UserPoolId=deployment.pool_id, ClientId=deployment.client_id)
    original = effective_settings(client_settings(described["UserPoolClient"]))
    tokens: dict = {}
    try:
        cognito.update_user_pool_client(
            UserPoolId=deployment.pool_id, ClientId=deployment.client_id, **shortened_settings(original)
        )
        tokens = _mint_tokens(
            deployment.session.region_name, deployment.pool_id, deployment.client_id, user.username, user.password
        )
    finally:
        restored, last_error = False, ""
        for attempt in range(5):
            try:
                cognito.update_user_pool_client(
                    UserPoolId=deployment.pool_id, ClientId=deployment.client_id, **original
                )
                now = cognito.describe_user_pool_client(UserPoolId=deployment.pool_id, ClientId=deployment.client_id)
                restored = effective_settings(client_settings(now["UserPoolClient"])) == original
                if restored:
                    break
                last_error = "the client's settings differ from the ones read before the change"
            except Exception as error:  # noqa: BLE001 - retried, then reported by the caller
                last_error = f"{type(error).__name__}: {error}"
            time.sleep(2 * (attempt + 1))
    if not restored:
        last_error += f"; restore these settings by hand: {json.dumps(original, default=str)}"
    return tokens, restored, last_error


def identity_session(deployment: rbac.Deployment, user: rbac.RoleUser):
    """The user's identity-pool credentials as a boto3 session, or None when the pool gives none."""
    from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

    try:
        return rbac.identity_pool_role(deployment, user)[1]
    except ClientError:
        return None


def unauthenticated_identity_refused(deployment: rbac.Deployment) -> tuple[str, str]:
    """(observed, detail) for an identity pool request with no login."""
    import boto3  # pylint: disable=import-outside-toplevel
    from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

    identity = boto3.client("cognito-identity", region_name=deployment.session.region_name)
    try:
        identity.get_id(IdentityPoolId=deployment.identity_pool_id)
    except ClientError as error:
        return "refused", error.response.get("Error", {}).get("Code", "")
    return "answered", "an identity id was issued with no login"


def self_sign_up_outcome(deployment: rbac.Deployment, allowed_domains: list[str]) -> tuple[str, str, str]:
    """(expected, observed, detail) for a self sign-up from a domain outside the allowed list.

    The address is in .invalid, which no allowed domain can be, so the sign-up
    must be refused whether or not self-registration is on. A user it creates
    anyway is deleted here.
    """
    import boto3  # pylint: disable=import-outside-toplevel
    from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

    username = f"{USER_PREFIX}signup-{uuid.uuid4().hex[:8]}@example.invalid"
    cognito = boto3.client("cognito-idp", region_name=deployment.session.region_name)
    try:
        cognito.sign_up(
            ClientId=deployment.client_id,
            Username=username,
            Password=rbac._password(),  # pylint: disable=protected-access
            UserAttributes=[{"Name": "email", "Value": username}],
        )
    except ClientError as error:
        return "refused", "refused", error.response.get("Error", {}).get("Code", "")
    deployment.session.client("cognito-idp").admin_delete_user(UserPoolId=deployment.pool_id, Username=username)
    mode = "domains " + ",".join(allowed_domains) if allowed_domains else "no allowed domain"
    return "refused", "answered", f"a sign-up from outside the allowed domains was accepted ({mode})"


# ── the throwaway meeting ──────────────────────────────────────────────────

GET_CALL = "query ($id: ID!) { getCall(CallId: $id) { CallId Owner SharedWith } }"
GET_SEGMENTS = (
    "query ($id: ID!) { getTranscriptSegments(callId: $id) { TranscriptSegments { SegmentId } } }"
)
GET_SENTIMENT = (
    "query ($id: ID!) { getTranscriptSegmentsWithSentiment(callId: $id) "
    "{ TranscriptSegmentsWithSentiment { SegmentId } } }"
)
GET_KEYFRAMES = (
    "query ($id: ID!) { getVideoKeyframes(callId: $id) { VideoKeyframes { FrameId ImageSignedUrl } } }"
)
GET_ATTACHMENTS = "query ($id: ID!) { getChatAttachments(callId: $id) { __typename } }"
GET_CONSENT = "query ($id: ID!) { getMeetingConsent(CallId: $id) { __typename } }"
GET_COST = "query ($id: ID!) { getCallCost(CallId: $id) { __typename } }"
LIST_WHITEBOARDS = "query ($id: ID!) { listMeetingWhiteboards(CallId: $id) { __typename } }"
LIST_NOTIFICATIONS = "query ($id: ID!) { listMeetingNotifications(CallId: $id) { __typename } }"
PREPARE_ARCHIVE = "mutation ($id: ID!) { prepareMeetingArchive(CallId: $id) { __typename } }"
LIST_CALLS = (
    "query ($s: AWSDateTime!, $e: AWSDateTime!, $n: String) "
    "{ listCallsDateRange(startDateTime: $s, endDateTime: $e, nextToken: $n) { Calls { CallId } nextToken } }"
)
UPDATE_LABELS = (
    "mutation ($id: ID!, $labels: [CallLabelInput!]!) "
    "{ updateCallLabels(input: {CallId: $id, Labels: $labels}) { CallId } }"
)
SHARE = (
    "mutation ($calls: [ShareMeetingsItem], $to: String) "
    "{ shareMeetings(input: {Calls: $calls, MeetingRecipients: $to}) { Result } }"
)
DELETE = "mutation ($calls: [DeleteMeetingsItem]) { deleteMeetings(input: {Calls: $calls}) { Result } }"
ADD_SEGMENT = (
    "mutation ($input: AddTranscriptSegmentInput!) { addTranscriptSegment(input: $input) { SegmentId } }"
)
ADD_KEYFRAME = "mutation ($input: AddVideoKeyframeInput!) { addVideoKeyframe(input: $input) { FrameId } }"

MEETING_WAIT_SECONDS = 120
SHARE_WAIT_SECONDS = 90
DELETE_WAIT_SECONDS = 120


class ProbeError(RuntimeError):
    pass


@dataclass
class Meeting:
    """One meeting created for the run through the meeting pipeline, owned by ``owner``."""

    deployment: rbac.Deployment
    owner: rbac.RoleUser
    call_id: str = ""
    segment_id: str = "integ-sectest-seg-1"
    frame_id: str = "integ-sectest-frame-1"
    list_keys: dict = field(default_factory=dict)
    created_at: str = ""

    @property
    def table(self) -> str:
        return self.deployment.ai_resources["EventSourcingTable"]

    @property
    def bucket(self) -> str:
        return self.deployment.outputs["RecordingsS3Bucket"]

    def row(self) -> dict | None:
        key = {"PK": {"S": f"c#{self.call_id}"}, "SK": {"S": f"c#{self.call_id}"}}
        ddb = self.deployment.session.client("dynamodb")
        return ddb.get_item(TableName=self.table, Key=key, ConsistentRead=True).get("Item")

    def create(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.call_id = f"integ-sectest-{stamp}-{uuid.uuid4().hex[:8]}"
        self.deployment.session.client("kinesis").put_record(
            StreamName=self.deployment.outputs["CallDataStreamName"],
            PartitionKey=self.call_id,
            Data=json.dumps(
                {
                    "EventType": "START",
                    "CallId": self.call_id,
                    "CustomerPhoneNumber": "Integ Security Test",
                    "SystemPhoneNumber": "LMA Integ Test",
                    # With no access token the processor makes AgentId the Owner.
                    "AgentId": self.owner.username,
                    "CreatedAt": datetime.now(timezone.utc).isoformat(),
                    "AccessToken": "",
                    "IdToken": "",
                    "RefreshToken": "",
                }
            ).encode(),
        )
        deadline = time.monotonic() + MEETING_WAIT_SECONDS
        while time.monotonic() < deadline:
            row = self.row()
            if row:
                self.created_at = row["CreatedAt"]["S"]
                shard = int(self.created_at[11:13]) // 4
                self.list_keys = {
                    "ListPK": f"cls#{self.created_at[:10]}#s#{shard:02d}",
                    "ListSK": f"ts#{self.created_at}#id#{self.call_id}",
                }
                if (row.get("Owner") or {}).get("S") != self.owner.username:
                    raise ProbeError(f"the meeting's Owner is {row.get('Owner')}, not the throwaway owner")
                return
            time.sleep(3)
        raise ProbeError("the meeting was not created; see test_kds_pipeline_creates_meeting")

    def seed(self) -> None:
        """A transcript segment and a key frame, written as the meeting pipeline writes them."""
        region = self.deployment.session.region_name
        segment = {
            "CallId": self.call_id,
            "Status": "TRANSCRIBING",
            "SegmentId": self.segment_id,
            "StartTime": 0.0,
            "EndTime": 1.0,
            "Transcript": "integration security test line",
            "IsPartial": False,
            "Channel": "REMOTE",
            "Speaker": "Integ",
            "Owner": self.owner.username,
        }
        folder = f"lma-video-keyframes/{posixify(self.call_id)}"
        image = f"https://{self.bucket}.s3.{region}.amazonaws.com/{folder}/frame-1.jpg"
        frame = {
            "CallId": self.call_id,
            "FrameId": self.frame_id,
            "Timestamp": 0.5,
            "ImageUrl": image,
            "ThumbnailUrl": image,
            "Owner": self.owner.username,
        }
        for document, variables in ((ADD_SEGMENT, {"input": segment}), (ADD_KEYFRAME, {"input": frame})):
            status, body = sigv4_graphql(self.deployment.session, self.deployment.graphql_url, document, variables)
            if status != 200 or body.get("errors"):
                raise ProbeError(f"seeding the meeting failed (HTTP {status}): {error_text(body)}")

    def calls_input(self) -> list[dict]:
        return [{"CallId": self.call_id, **self.list_keys}]

    def shared_with(self) -> str:
        value = (self.row() or {}).get("SharedWith") or {}
        if "L" in value:
            return ",".join(v.get("S", "") for v in value["L"])
        return value.get("S") or ",".join(value.get("SS", []))

    def labels(self) -> str:
        return json.dumps((self.row() or {}).get("Labels"), sort_keys=True)

    def cleanup(self) -> None:
        """Remove whatever is left of the meeting by its CallId: rows, list entry and objects."""
        if not self.call_id:
            return
        ddb = self.deployment.session.client("dynamodb")
        for pk in (f"c#{self.call_id}", f"trs#{self.call_id}", f"kf#{self.call_id}", f"nt#{self.call_id}"):
            start = None
            while True:
                kwargs = {
                    "TableName": self.table,
                    "KeyConditionExpression": "PK = :pk",
                    "ExpressionAttributeValues": {":pk": {"S": pk}},
                    "ProjectionExpression": "PK, SK",
                }
                if start:
                    kwargs["ExclusiveStartKey"] = start
                page = ddb.query(**kwargs)
                for item in page.get("Items", []):
                    ddb.delete_item(TableName=self.table, Key={"PK": item["PK"], "SK": item["SK"]})
                start = page.get("LastEvaluatedKey")
                if not start:
                    break
        if self.list_keys:
            ddb.delete_item(
                TableName=self.table,
                Key={"PK": {"S": self.list_keys["ListPK"]}, "SK": {"S": self.list_keys["ListSK"]}},
            )
        s3 = self.deployment.session.client("s3")
        for prefix in recording_probe_keys(self.call_id).values():
            s3.delete_object(Bucket=self.bucket, Key=prefix)


def recording_probe_keys(call_id: str) -> dict[str, str]:
    """An object under each recordings-bucket prefix the probe reads, keyed by what the prefix holds."""
    return {
        "audio recording": f"lma-audio-recordings/{call_id}.wav",
        "video recording": f"lma-video-recordings/{call_id}.webm",
        "transcript file": f"lma-transcripts/{call_id}.txt",
        "pending upload": f"lma-uploads-pending/{call_id}.wav",
    }


def as_user(deployment: rbac.Deployment, user: rbac.RoleUser, document: str, variables: dict) -> tuple[int, dict]:
    return graphql_raw(deployment.graphql_url, {"Authorization": user.tokens["id"]}, document, variables)


def segment_ids(body: dict, field_name: str) -> list[str]:
    data = ((body or {}).get("data") or {}).get(field_name) or {}
    items = data.get("TranscriptSegments") or data.get("TranscriptSegmentsWithSentiment") or []
    return [s.get("SegmentId") for s in items if s]


def posixify(call_id: str) -> str:
    """A meeting's folder name in the recordings bucket: its CallId made safe as a file name.

    The rule producers and meeting deletion use (posixify_filename in the
    key-frame resolver); a key frame is signed only inside this folder.
    """
    name = re.sub(r"[^a-zA-Z0-9_.]", "_", call_id)
    return re.sub(r"_+$", "", re.sub(r"^_+", "", name))


def keyframes(body: dict) -> list[dict]:
    data = ((body or {}).get("data") or {}).get("getVideoKeyframes") or {}
    return [f for f in data.get("VideoKeyframes") or [] if f]


def listed_call_ids(deployment: rbac.Deployment, user: rbac.RoleUser, meeting: Meeting) -> tuple[list[str], str]:
    """The CallIds listCallsDateRange answers the user for the hour around the meeting, and any error."""
    created = datetime.fromisoformat(meeting.created_at.replace("Z", "+00:00"))
    variables: dict[str, Any] = {
        "s": (created - timedelta(minutes=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "e": (created + timedelta(minutes=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "n": None,
    }
    found: list[str] = []
    for _ in range(50):
        _, body = as_user(deployment, user, LIST_CALLS, variables)
        data = (body.get("data") or {}).get("listCallsDateRange") or {}
        found += [c.get("CallId") for c in data.get("Calls") or [] if c]
        if body.get("errors") or not data.get("nextToken"):
            return found, error_text(body)
        variables["n"] = data["nextToken"]
    return found, ""


def wait_until(predicate, seconds: float, interval: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


# ── the WebSocket transcriber ──────────────────────────────────────────────


def transcriber_upgrade_status(deployment: rbac.Deployment, authorization: str | None) -> int:
    """The HTTP status of an upgrade with the given authorization value (None: no parameter)."""
    import websockets  # pylint: disable=import-outside-toplevel
    from urllib.parse import quote  # pylint: disable=import-outside-toplevel

    url = deployment.outputs["LMAWebsocketEndpoint"]
    if authorization is not None:
        url = f"{url}?authorization={quote(authorization)}"

    async def attempt() -> int:
        try:
            async with websockets.connect(url, open_timeout=25):
                return 101
        except websockets.exceptions.InvalidStatus as error:
            return error.response.status_code

    return asyncio.run(attempt())


def transcriber_start_on(deployment: rbac.Deployment, user: rbac.RoleUser, call_id: str) -> tuple[str, str]:
    """(observed, detail) for a START naming ``call_id`` from ``user``: refused or accepted."""
    import websockets  # pylint: disable=import-outside-toplevel

    url = f"{deployment.outputs['LMAWebsocketEndpoint']}?authorization=Bearer%20{user.tokens['access']}"

    async def attempt() -> tuple[str, str]:
        async with websockets.connect(url, open_timeout=25) as ws:
            await ws.send(
                json.dumps(
                    {
                        "callEvent": "START",
                        "callId": call_id,
                        "remoteSpeakerName": "Integ Security Test",
                        "localSpeakerName": user.username,
                        "samplingRate": 16000,
                    }
                )
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                except websockets.exceptions.ConnectionClosed as closed:
                    return ("refused" if closed.code == 1008 else "closed"), f"close {closed.code}"
                if isinstance(message, bytes):
                    continue
                frame = json.loads(message)
                if frame.get("event") == "CONSENT_STATE" and frame.get("captureAllowed") is False:
                    return "refused", str(frame.get("code"))
            # No refusal: end what was started so nothing keeps streaming.
            await ws.send(json.dumps({"callEvent": "END", "callId": call_id}))
            return "accepted", "no refusal within 20 seconds"

    return asyncio.run(attempt())


# ── the MCP server ─────────────────────────────────────────────────────────

GENERATE_KEY = "mutation ($s: String) { generateMCPApiKey(scope: $s) { keyValue keyPrefix } }"
REVOKE_KEY = "mutation ($p: String!) { revokeMCPApiKey(keyPrefix: $p) }"


def jsonrpc(url: str, headers: dict, method: str, params: dict | None = None) -> tuple[int, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    status, _, raw = http("POST", url, {"Content-Type": "application/json", **headers}, json.dumps(body).encode())
    try:
        return status, json.loads(raw or b"null")
    except json.JSONDecodeError:
        return status, None


def mcp_transcript_ids(answer: Any) -> tuple[list[str], str]:
    """The segment ids a get_meeting_transcript answer holds, and its error text if it is an error."""
    result = (answer or {}).get("result") if isinstance(answer, dict) else None
    if not isinstance(result, dict):
        return [], json.dumps(answer)[:200]
    text = ((result.get("content") or [{}])[0] or {}).get("text") or ""
    if result.get("isError"):
        return [], text[:200]
    try:
        segments = json.loads(text).get("segments") or []
    except (json.JSONDecodeError, AttributeError):
        return [], text[:200]
    return [s.get("segmentId") for s in segments if isinstance(s, dict)], ""


@dataclass
class McpKey:
    """An MCP API key generated by a user for the run, revoked when the block ends."""

    deployment: rbac.Deployment
    user: rbac.RoleUser
    value: str = ""
    prefix: str = ""

    def __enter__(self) -> "McpKey":
        status, body = as_user(self.deployment, self.user, GENERATE_KEY, {"s": "read"})
        generated = (body.get("data") or {}).get("generateMCPApiKey")
        if status != 200 or not generated:
            raise ProbeError(f"generateMCPApiKey failed (HTTP {status}): {error_text(body)}")
        self.value, self.prefix = generated["keyValue"], generated["keyPrefix"]
        return self

    def revoke(self) -> None:
        if self.prefix:
            as_user(self.deployment, self.user, REVOKE_KEY, {"p": self.prefix})
            self.prefix = ""

    def __exit__(self, *_exc) -> None:
        self.revoke()


# ── the Virtual Participant viewer path ────────────────────────────────────


def vnc_path_status(deployment: rbac.Deployment, token: str | None) -> int:
    base = deployment.outputs["ApplicationCloudfrontEndpoint"].rstrip("/")
    url = f"{base}/vnc/integ-sectest-{uuid.uuid4().hex[:8]}/vnc.html"
    if token is not None:
        url += f"?token={token}"
    status, _, _ = http("GET", url)
    return status


def forged_vnc_token() -> str:
    from jwt_tampering import b64url_encode  # pylint: disable=import-outside-toplevel

    payload = b64url_encode(json.dumps({"vpId": "integ-sectest", "prefix": "/vnc/", "exp": time.time() + 60}).encode())
    return f"{payload}.{b64url_encode(b'not-a-signature-of-this-payload')}"


# ── stack inventory ────────────────────────────────────────────────────────


def stack_resources_of_type(deployment: rbac.Deployment, stack_name: str, resource_type: str) -> list[str]:
    """Physical ids of every resource of the type in the stack and its nested stacks."""
    cfn = deployment.session.client("cloudformation")
    found: list[str] = []
    pending = [stack_name]
    while pending:
        current = pending.pop()
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=current):
            for resource in page["StackResourceSummaries"]:
                if resource["ResourceType"] == "AWS::CloudFormation::Stack" and resource.get("PhysicalResourceId"):
                    pending.append(resource["PhysicalResourceId"])
                elif resource["ResourceType"] == resource_type and resource.get("PhysicalResourceId"):
                    found.append(resource["PhysicalResourceId"])
    return found


def function_urls(deployment: rbac.Deployment, stack_name: str) -> list[str]:
    """The stack's Lambda functions that have a function URL."""
    from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

    lam = deployment.session.client("lambda")
    with_url: list[str] = []
    for name in stack_resources_of_type(deployment, stack_name, "AWS::Lambda::Function"):
        try:
            if lam.list_function_url_configs(FunctionName=name).get("FunctionUrlConfigs"):
                with_url.append(name)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
    return with_url


def unauthorized_rest_methods(deployment: rbac.Deployment, stack_name: str) -> tuple[int, list[str]]:
    """(methods checked, methods other than OPTIONS whose authorization type is NONE) of the stack's REST APIs."""
    apigw = deployment.session.client("apigateway")
    checked, open_methods = 0, []
    for api_id in stack_resources_of_type(deployment, stack_name, "AWS::ApiGateway::RestApi"):
        for page in apigw.get_paginator("get_resources").paginate(restApiId=api_id, embed=["methods"]):
            for resource in page.get("items", []):
                for verb, method in (resource.get("resourceMethods") or {}).items():
                    checked += 1
                    if verb != "OPTIONS" and method.get("authorizationType", "NONE") == "NONE":
                        open_methods.append(f"{api_id} {verb} {resource.get('path')}")
    return checked, open_methods
