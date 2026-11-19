# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The hosted MCP server's meeting tools and key scopes, end to end.

Signs in as the Cognito test user and creates one synthetic meeting owned by
that user: a START event on the CallDataStream (as kds_pipeline_probe sends),
then transcript segments and a small recording object written directly, so
the test controls exactly what the transcript holds. Every call the probe makes
names that meeting; it touches no other.

It then generates MCP API keys for the test user, one at a time because a user
holds one key:

1. a key with the read scope, with which a write tool must be refused and must
   leave nothing behind;
2. a key with the read-write scope, with which it polls get_live_transcript
   with its cursor while segments are added, pushes guidance and reads it back,
   reads the recording link from get_meeting_record, and deletes the meeting
   with delete_meeting, first without confirm (nothing may be deleted) and then
   with it.

Each key is revoked in a ``finally``, and whatever is left of the meeting is
removed at the end, by its CallId only. The result is a dict of observations;
it never holds a key value. See docs/mcp-api-key-auth.md.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import boto3
import cognito_test_user
from mcp_api_key_probe import LIST_KEYS, REVOKE_KEY, ProbeError, _graphql, _graphql_url, _post
from ws_stream_probe import _mint_tokens, _outputs, _stack_resources

GENERATE_KEY = (
    "mutation Generate($scope: String) { generateMCPApiKey(scope: $scope) { keyValue keyPrefix scope } }"
)
MEETING_WAIT_SECONDS = 90
DELETE_WAIT_SECONDS = 120


def _claims(id_token: str) -> dict:
    """The ID token's claims, read without verifying it (Cognito just issued it)."""
    payload = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class Deployment:
    """What the probe needs from the stack, resolved once."""

    def __init__(self, stack_name: str, region: str | None):
        self.session = boto3.Session(region_name=region)
        cfn = self.session.client("cloudformation")
        self.outputs = _outputs(cfn, stack_name)
        self.endpoint = self.outputs.get("MCPServerApiKeyEndpoint")
        if not self.endpoint:
            return
        ai_stack = next(
            r["PhysicalResourceId"]
            for r in _stack_resources(cfn, stack_name)
            if r["LogicalResourceId"] == "AISTACK"
        )
        ai = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, ai_stack)}
        self.table = ai["EventSourcingTable"]
        lam = self.session.client("lambda")
        mcp_env = lam.get_function_configuration(FunctionName=ai["MCPServerAnalyticsFunction"])
        self.bucket = mcp_env["Environment"]["Variables"]["RECORDINGS_BUCKET"]
        controls_env = lam.get_function_configuration(FunctionName=ai["MeetingControlsResolverFunction"])
        self.recordings_prefix = controls_env["Environment"]["Variables"].get(
            "S3_RECORDINGS_PREFIX", "lma-audio-recordings/"
        )
        self.graphql_url = _graphql_url(self.outputs)
        self.ddb = self.session.client("dynamodb")
        self.s3 = self.session.client("s3")
        self.kinesis = self.session.client("kinesis")


class Probe:
    """One run of the scenario; observations go in ``self.observed``."""

    def __init__(self, deployment: Deployment, id_token: str, username: str):
        self.d = deployment
        self.id_token = id_token
        self.username = username
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.call_id = f"integ-test-mcp-{stamp}-{uuid.uuid4().hex[:8]}"
        self.recording_key = f"{self.d.recordings_prefix}{self.call_id}.wav"
        self.observed: dict[str, Any] = {"enabled": True, "call_id": self.call_id}

    # ── the meeting ────────────────────────────────────────────────────────

    def create_meeting(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        stream = self.d.outputs["CallDataStreamName"]
        self.d.kinesis.put_record(
            StreamName=stream,
            PartitionKey=self.call_id,
            Data=json.dumps(
                {
                    "EventType": "START",
                    "CallId": self.call_id,
                    "CustomerPhoneNumber": "Integ Test Participant",
                    "SystemPhoneNumber": "LMA Integ Test",
                    # With no access token the processor makes AgentId the Owner.
                    "AgentId": self.username,
                    "CreatedAt": now_iso,
                    "AccessToken": "",
                    "IdToken": "",
                    "RefreshToken": "",
                }
            ).encode(),
        )
        deadline = time.monotonic() + MEETING_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.meeting_row():
                return
            time.sleep(3)
        raise ProbeError("the synthetic meeting was not created; see test_kds_pipeline_creates_meeting")

    def meeting_row(self) -> dict | None:
        key = {"PK": {"S": f"c#{self.call_id}"}, "SK": {"S": f"c#{self.call_id}"}}
        return self.d.ddb.get_item(TableName=self.d.table, Key=key, ConsistentRead=True).get("Item")

    def put_segment(self, segment_id: str, start: float, end: float, text: str, channel="REMOTE") -> None:
        self.d.ddb.put_item(
            TableName=self.d.table,
            Item={
                "PK": {"S": f"trs#{self.call_id}"},
                "SK": {"S": f"s#{segment_id}"},
                "CallId": {"S": self.call_id},
                "SegmentId": {"S": segment_id},
                "StartTime": {"N": str(start)},
                "EndTime": {"N": str(end)},
                "Transcript": {"S": text},
                "Channel": {"S": channel},
                "Speaker": {"S": "Integ Speaker"},
                "IsPartial": {"BOOL": False},
                "CreatedAt": {"S": datetime.now(timezone.utc).isoformat()},
                "Owner": {"S": self.username},
                "ExpiresAfter": {"N": str(int(time.time()) + 86400)},
            },
        )

    def put_recording(self) -> None:
        self.d.s3.put_object(Bucket=self.d.bucket, Key=self.recording_key, Body=b"RIFF0000WAVE")
        region = self.d.session.region_name
        url = f"https://{self.d.bucket}.s3.{region}.amazonaws.com/{self.recording_key}"
        key = {"PK": {"S": f"c#{self.call_id}"}, "SK": {"S": f"c#{self.call_id}"}}
        self.d.ddb.update_item(
            TableName=self.d.table,
            Key=key,
            UpdateExpression="SET RecordingUrl = :u",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":u": {"S": url}},
        )

    def cleanup(self) -> None:
        """Remove whatever is left of this probe's meeting, by its CallId only."""
        for pk in (f"c#{self.call_id}", f"trs#{self.call_id}", f"gd#{self.call_id}"):
            items = self.d.ddb.query(
                TableName=self.d.table,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": {"S": pk}},
            ).get("Items", [])
            for item in items:
                self.d.ddb.delete_item(TableName=self.d.table, Key={"PK": item["PK"], "SK": item["SK"]})
        created = (self.observed.get("created_at") or "")
        if len(created) >= 13:
            shard = int(created[11:13]) // 4
            self.d.ddb.delete_item(
                TableName=self.d.table,
                Key={
                    "PK": {"S": f"cls#{created[:10]}#s#{shard:02d}"},
                    "SK": {"S": f"ts#{created}#id#{self.call_id}"},
                },
            )
        # Only if delete_meeting left it: on a versioned bucket a second delete
        # would add a delete marker.
        try:
            self.d.s3.head_object(Bucket=self.d.bucket, Key=self.recording_key)
        except self.d.s3.exceptions.ClientError:
            return
        self.d.s3.delete_object(Bucket=self.d.bucket, Key=self.recording_key)

    # ── keys and tools ─────────────────────────────────────────────────────

    def generate_key(self, scope: str) -> tuple[str, str]:
        generated = _graphql(self.d.graphql_url, self.id_token, GENERATE_KEY, {"scope": scope})
        generated = generated["generateMCPApiKey"]
        if generated.get("scope") != scope:
            raise ProbeError(f"generateMCPApiKey answered scope {generated.get('scope')!r} for {scope!r}")
        return generated["keyValue"], generated["keyPrefix"]

    def revoke(self, prefix: str) -> None:
        _graphql(self.d.graphql_url, self.id_token, REVOKE_KEY, {"p": prefix})

    def tools(self, key: str) -> set[str]:
        status, body = _post(self.d.endpoint, {"x-api-key": key}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        if status != 200:
            raise ProbeError(f"tools/list returned HTTP {status}")
        return {tool["name"] for tool in body["result"]["tools"]}

    def call(self, key: str, name: str, arguments: dict) -> tuple[bool, Any]:
        """(is_error, the tool's answer as JSON, or its error text)."""
        status, body = _post(
            self.d.endpoint,
            {"x-api-key": key},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        )
        if status != 200 or not isinstance(body, dict) or "result" not in body:
            raise ProbeError(f"{name} returned HTTP {status}: {json.dumps(body)[:300]}")
        result = body["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return True, text
        return False, json.loads(text)

    # ── the scenario ───────────────────────────────────────────────────────

    def with_read_key(self) -> None:
        key, prefix = self.generate_key("read")
        try:
            listed = self.tools(key)
            self.observed["read_key_lists_live_transcript"] = "get_live_transcript" in listed
            self.observed["read_key_lists_write_tool"] = bool(
                listed & {"push_meeting_guidance", "delete_meeting", "start_meeting_now"}
            )
            error, answer = self.call(key, "push_meeting_guidance", {"meeting_id": self.call_id, "text": "x"})
            self.observed["read_key_write_refused"] = error and "read-write" in str(answer)
            error, answer = self.call(key, "delete_meeting", {"meeting_id": self.call_id, "confirm": True})
            self.observed["read_key_delete_refused"] = error
            error, answer = self.call(key, "get_live_transcript", {"meeting_id": self.call_id})
            self.observed["read_key_reads"] = not error
        finally:
            self.revoke(prefix)
        self.observed["guidance_after_read_key"] = self._guidance_rows()
        self.observed["meeting_after_read_key"] = self.meeting_row() is not None

    def _guidance_rows(self) -> int:
        return self.d.ddb.query(
            TableName=self.d.table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": f"gd#{self.call_id}"}},
            Select="COUNT",
        )["Count"]

    def with_read_write_key(self) -> None:
        key, prefix = self.generate_key("read-write")
        try:
            self._live_transcript(key)
            self._guidance(key)
            self._record(key)
            self._delete(key)
        finally:
            self.revoke(prefix)

    def _live_transcript(self, key: str) -> None:
        self.put_segment("seg-1", 0.0, 2.0, "first line")
        self.put_segment("seg-2", 2.5, 4.0, "second line", channel="LOCAL")
        _, first = self.call(key, "get_live_transcript", {"meeting_id": self.call_id})
        self.put_segment("seg-3", 4.5, 6.0, "third line")
        _, second = self.call(key, "get_live_transcript", {"meeting_id": self.call_id, "since": first["cursor"]})
        _, third = self.call(key, "get_live_transcript", {"meeting_id": self.call_id, "since": second["cursor"]})
        self.observed["live_first_ids"] = [line["segmentId"] for line in first["lines"]]
        self.observed["live_second_ids"] = [line["segmentId"] for line in second["lines"]]
        self.observed["live_third_ids"] = [line["segmentId"] for line in third["lines"]]
        self.observed["live_is_live"] = first["live"]

    def _guidance(self, key: str) -> None:
        text = f"integ guidance {uuid.uuid4().hex[:6]}"
        error, pushed = self.call(
            key, "push_meeting_guidance", {"meeting_id": self.call_id, "text": text, "level": "warning"}
        )
        self.observed["guidance_push_error"] = pushed if error else None
        _, listed = self.call(key, "list_meeting_guidance", {"meeting_id": self.call_id})
        self.observed["guidance_listed"] = any(
            item["text"] == text and item["level"] == "WARNING" for item in listed["guidance"]
        )

    def _record(self, key: str) -> None:
        self.put_recording()
        error, record = self.call(
            key, "get_meeting_record", {"meeting_id": self.call_id, "include": ["details", "recordings"]}
        )
        if error:
            self.observed["record_error"] = record
            return
        link = record["recordings"]["audioUrl"] or ""
        query = parse_qs(urlparse(link).query)
        self.observed["record_link_expires"] = query.get("X-Amz-Expires", [None])[0]
        self.observed["record_link_signed"] = "X-Amz-Signature" in query
        self.observed["record_links_expire_in"] = record.get("linksExpireInSeconds")
        status = _fetch_status(link)
        self.observed["record_link_fetch_status"] = status

    def _delete(self, key: str) -> None:
        error, _ = self.call(key, "delete_meeting", {"meeting_id": self.call_id})
        self.observed["delete_without_confirm_refused"] = error
        self.observed["meeting_after_unconfirmed_delete"] = self.meeting_row() is not None
        error, answer = self.call(key, "delete_meeting", {"meeting_id": self.call_id, "confirm": True})
        self.observed["delete_error"] = answer if error else None
        deadline = time.monotonic() + DELETE_WAIT_SECONDS
        while time.monotonic() < deadline and self.meeting_row():
            time.sleep(3)
        self.observed["meeting_after_delete"] = self.meeting_row() is not None


def _fetch_status(url: str) -> int | None:
    import urllib.error
    import urllib.request

    if not url.startswith("https://"):
        return None
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # nosec B310 - a pre-signed S3 link
            return response.status
    except urllib.error.HTTPError as err:
        return err.code


def run_probe(stack_name: str, region: str | None = None) -> dict:
    """Run the scenario and report what it observed."""
    credentials = cognito_test_user.resolve(region)
    if credentials is None:
        raise cognito_test_user.CredentialsUnavailable(
            f"no Cognito test user configured — {cognito_test_user.describe_sources()}"
        )
    deployment = Deployment(stack_name, region)
    if not deployment.endpoint:
        return {"enabled": False}
    username, password = credentials
    outputs = deployment.outputs
    pool_id = outputs.get("MCPServerUserPoolId") or outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    tokens = _mint_tokens(deployment.session.region_name, pool_id, outputs["CognitoUserPoolClientId"], username, password)
    owner = _claims(tokens["id"]).get("cognito:username") or username

    existing = _graphql(deployment.graphql_url, tokens["id"], LIST_KEYS)["listMCPApiKeys"] or []
    if existing:
        # One key per user, and the test cannot tell whether someone relies on it.
        return {"enabled": True, "existing_key_prefixes": [k["keyPrefix"] for k in existing]}

    probe = Probe(deployment, tokens["id"], owner)
    try:
        probe.create_meeting()
        probe.observed["created_at"] = probe.meeting_row()["CreatedAt"]["S"]
        probe.with_read_key()
        probe.with_read_write_key()
    finally:
        probe.cleanup()
    return probe.observed
