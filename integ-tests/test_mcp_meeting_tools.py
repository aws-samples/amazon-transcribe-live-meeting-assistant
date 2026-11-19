# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The hosted MCP server's meeting tools and API key scopes, on a live stack.

One probe run (mcp_meeting_tools_probe.py) creates a synthetic meeting owned by
the Cognito test user, generates a read key and then a read-write key, and
exercises the tools against that meeting only: a write tool refused to the read
key, get_live_transcript polled with its cursor, guidance pushed and listed, a
recording link from get_meeting_record, and delete_meeting with and without
confirm. Each test asserts one property of what it observed.

Runs whenever a Cognito test user is configured, like test_mcp_api_key_logging,
and skips without one. A stack deployed with EnableMCP=false has nothing to
check, so there it skips, marked ``optional_feature`` so that ``--no-skips`` does
not turn it into a failure.

Self-cleaning: both keys are revoked in a ``finally``, and whatever is left of
the meeting is removed by its CallId. If the test user already holds a key it
is left alone and the test fails, naming the key's prefix, because one user can
hold only one key.
"""

from __future__ import annotations

import cognito_test_user
import pytest


@pytest.fixture(scope="module")
def observed(client, request):
    if not cognito_test_user.available(client.region):
        pytest.skip(f"no Cognito test user — {cognito_test_user.describe_sources()}")
    try:
        import pycognito  # noqa: F401
    except ImportError:
        pytest.skip("pycognito not installed (pip install -r integ-tests/requirements.txt)")

    from mcp_meeting_tools_probe import run_probe

    result = run_probe(client.stack_name, region=client.region)
    if not result["enabled"]:
        request.node.add_marker(pytest.mark.optional_feature)
        pytest.skip(f"{client.stack_name} has no MCP API key endpoint (EnableMCP=false)")
    if result.get("existing_key_prefixes"):
        pytest.fail(
            f"the Cognito test user already holds an MCP API key (prefix "
            f"{result['existing_key_prefixes']}); revoke it from the MCP API Keys page and re-run"
        )
    return result


# ── key scopes ──────────────────────────────────────────────────────────────


def test_a_read_key_is_offered_only_read_tools(observed) -> None:
    assert observed["read_key_lists_live_transcript"], "tools/list did not offer get_live_transcript"
    assert not observed["read_key_lists_write_tool"], "tools/list offered a write tool to a read key"


def test_a_read_key_is_refused_a_write_tool_and_nothing_changes(observed) -> None:
    assert observed["read_key_write_refused"], "push_meeting_guidance ran for a read key"
    assert observed["read_key_delete_refused"], "delete_meeting ran for a read key"
    assert observed["guidance_after_read_key"] == 0, "a read key's refused push stored guidance"
    assert observed["meeting_after_read_key"], "a read key's refused delete removed the meeting"


def test_a_read_key_reads(observed) -> None:
    assert observed["read_key_reads"], "get_live_transcript was refused to a read key"


# ── get_live_transcript ─────────────────────────────────────────────────────


def test_live_transcript_cursor_returns_only_new_lines(observed) -> None:
    assert observed["live_first_ids"] == ["seg-1", "seg-2"], observed["live_first_ids"]
    assert observed["live_second_ids"] == ["seg-3"], observed["live_second_ids"]
    assert observed["live_third_ids"] == [], observed["live_third_ids"]
    assert observed["live_is_live"] is True, "a STARTED meeting was not reported live"


# ── push_meeting_guidance ───────────────────────────────────────────────────


def test_pushed_guidance_is_stored_on_the_meeting(observed) -> None:
    assert observed["guidance_push_error"] is None, observed["guidance_push_error"]
    assert observed["guidance_listed"], "list_meeting_guidance did not return the pushed note"


# ── get_meeting_record ──────────────────────────────────────────────────────


def test_meeting_record_links_are_time_limited(observed) -> None:
    assert "record_error" not in observed, observed.get("record_error")
    assert observed["record_link_signed"], "the recording link is not a pre-signed URL"
    assert observed["record_link_expires"] == "3600", observed["record_link_expires"]
    assert observed["record_links_expire_in"] == 3600
    assert observed["record_link_fetch_status"] == 200, observed["record_link_fetch_status"]


# ── delete_meeting ──────────────────────────────────────────────────────────


def test_delete_meeting_requires_confirm(observed) -> None:
    assert observed["delete_without_confirm_refused"], "delete_meeting ran without confirm"
    assert observed["meeting_after_unconfirmed_delete"], "the meeting went without confirm"


def test_delete_meeting_with_confirm_deletes_the_meeting(observed) -> None:
    assert observed["delete_error"] is None, observed["delete_error"]
    assert not observed["meeting_after_delete"], "the meeting row is still there after delete_meeting"
