# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting assistant's profile settings and the MCP server library, against a live stack.

Three behaviours, each from the outside:

- **The profile's assistant instructions reach an answer.** Meeting Assistant ›
  Assistant instructions (``meeting_assist.systemPrompt``) is set, in the active
  profile, to instructions that make every answer start with a marker word, and
  the assistant is asked until an answer does (a saved change can take up to the
  section's five-minute cache to apply).
- **A server flagged off for in-meeting requests is not offered to one.** With
  In-meeting requests on Selected tools, the MCP servers switch on, and one
  installed server's ``inMeetingRequests`` set to false, a meeting-chat request is
  not offered that server while the owner's own request still is, as the
  assistant's log line for each request records. Needs an installed, active MCP
  server; without one it fails saying so.
- **The MCP server library round-trips.** ``client.mcp.export_library()`` holds no
  credential; importing it back changes nothing but the update times (an unchanged
  package server starts no layer build, so the re-import rebuilds nothing); and a
  server imported from a library is created waiting for its credentials. On a
  long-lived stack, the round trip can also fail for a reason that is not a
  regression: a server stored without a ``Version`` comes back as ``latest``
  (and a package server then rebuilds), and a server whose stored package or id
  an install would refuse today is ``skipped``. Check those first.

Opt-in. It runs only with ``LMA_TEST_ASSISTANT_PROFILE=1``, because it changes
deployment-wide settings while it runs (put back in teardown), takes several
minutes, calls Amazon Bedrock, and writes a probe server into the MCP servers
table (removed in teardown):

    AWS_PROFILE=default LMA_TEST_ASSISTANT_PROFILE=1 \\
      make integ-tests STACK=lma-integtest1

The settings are written through the configuration store into the active
profile, as the consent and transcript-deletion tests write theirs.
"""

from __future__ import annotations

import json
import os
import time

import pytest

ENABLED = os.environ.get("LMA_TEST_ASSISTANT_PROFILE", "") not in ("", "0", "false")

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_ASSISTANT_PROFILE=1"),
]

MARKER = "PINEAPPLE"
INSTRUCTIONS = (
    "You are a meeting assistant used by an automated test. Begin every answer with the word "
    f"{MARKER} in capitals, followed by a colon, and then answer in one short sentence."
)
PROBE_SERVER_NAME = "LMA integration test library probe"
PROBE_SERVER_ID = "https://integ-tests.example.invalid/mcp"


@pytest.fixture(scope="module")
def stack(client):
    from meeting_consent_probe import Stack

    return Stack.find(client.stack_name, client.region)


@pytest.fixture(scope="module")
def meeting_assist(stack):
    """The Meeting Assistant section of the active profile, saved first and always put back."""
    from meeting_consent_probe import ConsentSettingsOverride

    override = ConsentSettingsOverride(stack, section="meeting_assist", updated_by="integ-tests:assistant-profile")
    override.save()
    try:
        yield override
    finally:
        override.restore()


def _ask_until(stack, predicate, question, **kwargs):
    from assistant_profile_probe import APPLY_POLL_SECONDS, APPLY_TIMEOUT_SECONDS, invoke_assistant

    deadline = time.monotonic() + APPLY_TIMEOUT_SECONDS
    answer = None
    while time.monotonic() < deadline:
        answer = invoke_assistant(stack.session, stack.name, question, **kwargs)
        assert not answer["function_error"], answer
        if predicate(answer):
            return answer
        time.sleep(APPLY_POLL_SECONDS)
    pytest.fail(f"no answer met the condition within {APPLY_TIMEOUT_SECONDS}s; the last was {answer}")


# ── the profile's instructions ─────────────────────────────────────────────


def test_the_profile_instructions_are_applied_to_an_answer(stack, meeting_assist):
    meeting_assist.apply(systemPrompt=INSTRUCTIONS)
    answer = _ask_until(
        stack,
        lambda a: a["message"].strip().upper().startswith(MARKER),
        "What is two plus two?",
        user_email="integ-tests@example.com",
    )
    assert MARKER in answer["message"].upper()


# ── per-server in-meeting flags ───────────────────────────────────────────


def test_a_server_flagged_off_for_in_meeting_requests_is_not_offered_to_one(stack, meeting_assist):
    from assistant_profile_probe import (
        APPLY_TIMEOUT_SECONDS,
        account_id,
        active_mcp_servers,
        assistant_log_group,
        invoke_assistant,
        mcp_servers_table,
        offered_mcp_servers,
    )

    servers = active_mcp_servers(stack.session, mcp_servers_table(stack.session, stack.name), account_id(stack.session))
    if not servers:
        pytest.fail("no active MCP server is installed on this stack; install one to run this test")
    server_id = servers[0]["ServerId"]
    meeting_assist.apply(
        inMeetingRequestTools="selected",
        inMeetingMcpServers=True,
        toolSelection={"mcpServers": {server_id: {"inMeetingRequests": False}}, "skills": {}},
    )
    log_group = assistant_log_group(stack.session, stack.name)

    deadline = time.monotonic() + APPLY_TIMEOUT_SECONDS
    seen = None
    while time.monotonic() < deadline:
        started = int(time.time() * 1000) - 1000
        owner = invoke_assistant(stack.session, stack.name, "Hello.", user_email="integ-tests@example.com")
        chat = invoke_assistant(stack.session, stack.name, "Hello.", tool_scope="meeting_chat")
        seen = {
            "owner": offered_mcp_servers(stack.session, log_group, owner["request_id"], started),
            "chat": offered_mcp_servers(stack.session, log_group, chat["request_id"], started),
        }
        if seen["chat"] is not None and server_id not in seen["chat"]["offered"]:
            break
        time.sleep(20)
    assert seen and seen["chat"] is not None, f"the in-meeting request logged no MCP server line: {seen}"
    assert server_id not in seen["chat"]["offered"], seen
    assert server_id in seen["chat"]["not_offered"], seen
    assert seen["owner"] is not None and server_id in seen["owner"]["offered"], seen


# ── the MCP server library ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def library_table(stack):
    """The MCP servers table, with the probe server removed again after the module."""
    from assistant_profile_probe import account_id, mcp_servers_table

    table = mcp_servers_table(stack.session, stack.name)
    account = account_id(stack.session)
    ddb = stack.session.client("dynamodb")
    key = {"AccountId": {"S": account}, "ServerId": {"S": PROBE_SERVER_ID}}
    ddb.delete_item(TableName=table, Key=key)
    try:
        yield table
    finally:
        # A remote server has no layer build to undo, so removing its row is the
        # whole of an uninstall.
        ddb.delete_item(TableName=table, Key=key)


def _no_credential_values(entry: dict) -> None:
    auth = entry.get("auth", {})
    assert auth.get("token", "") == "", entry["name"]
    for key in ("headers", "env"):
        assert set((auth.get(key) or {}).values()) <= {""}, entry["name"]
    oauth = auth.get("oauth") or {}
    for key in ("clientSecret", "accessToken", "refreshToken"):
        assert oauth.get(key, "") == "", entry["name"]


def test_the_library_export_holds_no_credentials(client, library_table):
    document = client.mcp.export_library()
    assert document["kind"] == "lma-mcp-server-library" and document["version"] == 1
    text = json.dumps(document)
    assert "AuthConfig" not in text
    for entry in document["servers"]:
        _no_credential_values(entry)


def test_a_library_import_round_trip_preserves_every_server(client, library_table):
    before = client.mcp.export_library()
    result = client.mcp.import_library(before)
    assert result["Success"] is True, result
    assert {s["Action"] for s in result["Servers"]} <= {"updated"}, result
    after = client.mcp.export_library()
    assert after["servers"] == before["servers"]


def test_an_imported_server_waits_for_its_credentials(client, library_table, stack):
    from assistant_profile_probe import account_id

    installed = client.mcp.export_library()["servers"]
    if len(installed) >= 5:
        pytest.fail("five MCP servers are installed, the per-account limit; remove one to run this test")
    document = {
        "kind": "lma-mcp-server-library",
        "version": 1,
        "servers": [
            {
                "serverId": PROBE_SERVER_ID,
                "name": PROBE_SERVER_NAME,
                "packageType": "streamable-http",
                "serverUrl": PROBE_SERVER_ID,
                "transport": ["streamable-http"],
                # A value in a credential field is ignored by the import.
                "auth": {"authType": "bearer", "token": "integ-tests-not-stored"},
            }
        ],
    }
    result = client.mcp.import_library(document)
    assert result["Servers"] == [
        {
            "Name": PROBE_SERVER_NAME,
            "ServerId": PROBE_SERVER_ID,
            "Action": "created",
            "CredentialsRequired": True,
            "Message": "",
        }
    ], result
    item = stack.session.client("dynamodb").get_item(
        TableName=library_table,
        Key={"AccountId": {"S": account_id(stack.session)}, "ServerId": {"S": PROBE_SERVER_ID}},
        ConsistentRead=True,
    )["Item"]
    assert item["CredentialsRequired"]["BOOL"] is True
    assert "AuthConfig" not in item
    assert "integ-tests-not-stored" not in json.dumps(item)
    exported = next(s for s in client.mcp.export_library()["servers"] if s["name"] == PROBE_SERVER_NAME)
    assert exported["auth"] == {"authType": "bearer", "token": ""}
