# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The hosted MCP server's meeting tools and API key scopes, on a live stack.

One probe run (mcp_meeting_tools_probe.py) creates a synthetic meeting owned by
the Cognito test user, generates a read key and then a read-write key, and
exercises the tools against that meeting only: a write tool refused to the read
key, search_lma_meetings bounded by startDate and endDate (which reads every
meeting the user can search, not only that one), get_meeting_transcript read
page by page with its nextToken, guidance pushed and listed, the meeting's
agenda, context, checklist, labels and audience set and read (and a label
value with a line break refused), the meeting found by its labels, its consent
record, sharing added and removed, a recording link from get_meeting_record,
delete_meeting with and without confirm, schedule_meeting called again for a
meeting it already scheduled and with meeting preparation,
update_scheduled_meeting, and end_virtual_participant called twice.
Each test asserts one property of what it observed.

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
    assert observed["read_key_lists_read_tools"], "tools/list did not offer the read tools"
    assert not observed["read_key_lists_write_tool"], "tools/list offered a write tool to a read key"
    assert not observed["read_key_lists_preparation_writes"], "a read key was offered a preparation write"


def test_a_read_key_is_refused_a_write_tool_and_nothing_changes(observed) -> None:
    assert observed["read_key_write_refused"], "push_meeting_guidance ran for a read key"
    assert observed["read_key_delete_refused"], "delete_meeting ran for a read key"
    assert observed["read_key_context_refused"], "set_meeting_context ran for a read key"
    assert observed["guidance_after_read_key"] == 0, "a read key's refused push stored guidance"
    assert observed["meeting_after_read_key"], "a read key's refused delete removed the meeting"


def test_a_read_key_reads(observed) -> None:
    assert observed["read_key_reads"], "get_meeting_transcript was refused to a read key"


# ── search_lma_meetings, bounded by date ────────────────────────────────────


def test_a_date_bounded_search_is_answered(observed) -> None:
    """startDate and endDate become numeric conditions the knowledge base accepts."""
    search = observed["date_search"]
    if search["error"] and "Knowledge Base not configured" in search["error"]:
        pytest.skip("the stack has no transcript knowledge base")
    assert search["error"] is None, search["error"]
    assert search["answered"], "search_lma_meetings answered without an answer field"


def test_a_search_date_that_is_no_date_is_refused_naming_the_argument(observed) -> None:
    """Checked before the knowledge base and the model, so on every stack."""
    refused = observed["date_search_bad_date"]
    assert refused["error"], refused["text"]
    assert "startDate must be an ISO 8601 date" in refused["text"], refused["text"]


# ── get_meeting_transcript, page by page ────────────────────────────────────


def test_transcript_pages_return_each_segment_once(observed) -> None:
    assert observed["pages_ids"] == [["seg-1"], ["seg-2"], ["seg-3"], []], observed["pages_ids"]
    assert observed["pages_status"] == "STARTED", observed["pages_status"]
    assert observed["pages_live_token"], "a live meeting's page had no nextToken to poll from"
    for field in ("segmentId", "startTime", "endTime", "speaker", "channel", "text", "type"):
        assert field in observed["pages_segment_fields"], field


# ── meeting preparation ─────────────────────────────────────────────────────


def test_the_preparation_tools_answer_without_error(observed) -> None:
    assert observed["preparation_errors"] == {}, observed["preparation_errors"]


def test_agenda_and_context_are_written_and_read_back(observed) -> None:
    assert observed["agenda_written"] == ("Intro\nBudget", "mcp"), observed["agenda_written"]
    assert observed["agenda_read"] == "Intro\nBudget"
    assert observed["context_read"] == "# Goals\nAgree the budget"
    assert observed["context_updated_by"], "the context change names no one"


def test_a_checklist_item_status_is_the_users_and_survives_a_replace(observed) -> None:
    assert observed["checklist_ids"] == ["q1", "t1"], observed["checklist_ids"]
    assert observed["item_status"] == ("covered", "user", "Dana"), observed["item_status"]
    assert observed["checklist_kept_status"] == [("q1", "covered")], observed["checklist_kept_status"]


def test_labels_find_the_meeting_only_when_all_match(observed) -> None:
    labels, audience = observed["metadata"]
    assert audience == "internal" and labels and labels.get("kind") == "mcp", observed["metadata"]
    assert observed["listed_by_labels"] == [observed["call_id"]], observed["listed_by_labels"]
    assert observed["listed_by_one_wrong_label"] == [], observed["listed_by_one_wrong_label"]
    for field in ("labels", "audience", "status", "startTime", "endTime", "owner", "summaryReady"):
        assert field in observed["listed_fields"], field


def test_label_values_are_stored_matched_and_returned_unchanged(observed) -> None:
    opaque = observed["opaque_label"]
    labels, _audience = observed["metadata"]
    assert labels.get("opaque") == opaque, labels
    assert (observed["listed_labels"] or {}).get("opaque") == opaque, observed["listed_labels"]
    assert observed["listed_by_opaque_label"] == [observed["call_id"]], observed["listed_by_opaque_label"]
    assert observed["listed_by_trimmed_label"] == [], observed["listed_by_trimmed_label"]


def test_a_request_over_a_limit_is_refused_whole_naming_the_limit(observed) -> None:
    assert observed["too_many_labels"] and "at most 20 labels" in observed["too_many_labels"], observed[
        "too_many_labels"
    ]
    assert observed["too_many_items"] and "at most 100 items" in observed["too_many_items"], observed[
        "too_many_items"
    ]
    refused = observed["label_with_line_break"]
    assert refused and "must not contain a line break" in refused, refused
    # Nothing of the refused label changes was applied.
    assert observed["labels_after_refusals"] == observed["metadata"][0], observed["labels_after_refusals"]


def test_a_meeting_without_consent_reports_mode_off(observed) -> None:
    consent = observed["consent"]
    assert consent.get("mode") == "off", consent
    assert consent.get("rule") is None and consent.get("status") == "not_required", consent
    assert consent.get("notices") == [] and consent.get("events") == [], consent
    assert consent.get("hasEvidence") is False and "captureAllowed" in consent, consent


def test_the_consent_record_is_given_in_lma_s_own_values(observed) -> None:
    assert not observed.get("consent_write_error"), observed.get("consent_write_error")
    consent = observed["consent_started"]
    assert "error" not in consent, consent
    assert (consent["mode"], consent["rule"]) == ("opt_in", "each_participant"), consent
    assert consent["status"] in ("pending", "granted"), consent
    assert isinstance(consent["captureAllowed"], bool), consent
    assert [notice["channel"] for notice in consent["notices"]] == ["chat"], consent["notices"]
    assert consent["notices"][0]["postedAt"], consent["notices"]
    replies = [event for event in consent["events"] if event["source"] != "attestation"]
    assert replies == [
        {
            "participantName": "Integ Consent Participant",
            "decision": "consent",
            # A signed-in user's entry is recorded as made on the page.
            "source": "ui",
            "at": replies[0]["at"] if replies else None,
            "speakerId": None,
            # A reply made on the page is not linked to a transcript speaker.
            "speakerLinkBasis": None,
            "speakerLinkedAt": None,
            # A reply made on the page is not a consent given in advance.
            "matchBasis": None,
            "matchedNameSource": None,
            "givenInAdvance": None,
        }
    ], consent["events"]
    assert replies[0]["at"], replies
    assert consent["hasEvidence"] is False, consent
    for removed in ("consentGivenBy", "participants", "evidence"):
        assert removed not in consent, consent


def test_share_meeting_adds_and_removes(observed) -> None:
    assert observed["shared_after_add"], "share_meeting did not add the address"
    assert not observed["shared_after_remove"], "share_meeting action remove left the address"


# ── push_meeting_guidance ───────────────────────────────────────────────────


def test_pushed_guidance_is_stored_on_the_meeting(observed) -> None:
    assert observed["guidance_push_error"] is None, observed["guidance_push_error"]
    assert observed["guidance_listed"], "list_meeting_guidance did not return the pushed note"


def test_pushed_guidance_is_a_guidance_notification(observed) -> None:
    """list_meeting_notifications returns the note with the fields a reader relies on."""
    listed = observed["notifications_listed"]
    assert len(listed) == 1, listed
    (note,) = listed
    assert note["category"] == "guidance" and note["type"] == "note"
    assert note["severity"] == "warning"
    assert note["refs"] == [] and "dedupKey" in note


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
    assert observed["guidance_after_delete"] == 0, (
        f"{observed['guidance_after_delete']} guidance row(s) left after delete_meeting"
    )


# ── schedule_meeting called again for the same meeting ──────────────────────


def test_scheduling_the_same_meeting_again_updates_its_virtual_participant(observed) -> None:
    assert observed.get("schedule_error") is None, observed.get("schedule_error")
    assert observed["schedule_actions"] == ["created", "updated", "created"], observed["schedule_actions"]
    assert observed["schedule_second_kept_id"], "the second call scheduled a second Virtual Participant"
    assert observed["schedule_updated_meeting_id"] == "89876543210", observed["schedule_updated_meeting_id"]
    assert observed["schedule_duplicate_new_id"], "allowDuplicate did not schedule a new Virtual Participant"
    assert observed["schedule_lma_meeting_id"] is None, "a scheduled VP answered with an LMA meeting id"


def test_update_scheduled_meeting_changes_the_scheduled_virtual_participant(observed) -> None:
    assert "update_scheduled_error" not in observed, observed.get("update_scheduled_error")
    updated = observed["update_scheduled"]
    assert updated["agenda"] == "Updated agenda"
    assert updated["labels"] == {"probe": "updated"}
    assert updated["audience"] is None and updated["checklist"] == []
    assert updated["context"] == "Scheduled context", "a setting not given was changed"
    assert updated["shareWith"] == ["integ-share-2@example.com"]
    assert updated["meetingId"] is None


def test_end_virtual_participant_cancels_and_is_idempotent(observed) -> None:
    ends = observed["end_answers"]
    assert ends and all("error" not in end for end in ends), ends
    assert all(end["alreadyEnded"] is False for end in ends[:-1]), ends
    assert ends[-1]["alreadyEnded"] is True, ends
