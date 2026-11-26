# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting notifications on a live stack.

One probe run (meeting_notifications_probe.py) creates a synthetic meeting
owned by a synthetic user, publishes notifications to it over IAM with the
publish helper, and reads them as the Cognito test user before, during and
after the meeting is shared with them; then checks dedup, the user's own view,
the per-minute allowance, and deletion with the transcript and with the
meeting. Live delivery is checked over the AppSync real-time endpoint with the
web UI's own subscription, for a shared and an owned meeting, and, where
Playwright and its Chromium are installed, in the web UI itself (the top
navigation's count, and the Notifications drawer opening for a meeting's first
notification). Each test asserts one property of what it observed.

Runs whenever a Cognito test user is configured, and skips without one. The two
refusals to a user the meeting is not shared with skip when the test user is an
Admin, who reads every meeting.
Self-cleaning: whatever is left of the meeting is removed by its CallId, and a
category muted on every meeting for the test is unmuted again.
"""

from __future__ import annotations

import re

import cognito_test_user
import pytest
from meeting_notifications_probe import ui_subscription


@pytest.fixture(scope="module")
def observed(client):
    if not cognito_test_user.available(client.region):
        pytest.skip(f"no Cognito test user — {cognito_test_user.describe_sources()}")
    try:
        import pycognito  # noqa: F401
    except ImportError:
        pytest.skip("pycognito not installed (pip install -r integ-tests/requirements.txt)")

    from meeting_notifications_probe import run_probe

    return run_probe(client.stack_name, region=client.region)


# ── publishing ──────────────────────────────────────────────────────────────


def test_a_backend_publishes_and_the_recipients_come_from_the_meeting(observed) -> None:
    assert observed["published_created"] == [True, True], observed["published_created"]
    assert observed["published_owner"], "the published message carries no Owner"
    assert observed["published_owner"].startswith("integ-notifications-")
    # Shared with nobody yet, so the message reaches the owner and Admins only.
    assert observed["published_shared_with_before_share"] is None


# ── who sees them ───────────────────────────────────────────────────────────


def _skip_for_an_admin(observed) -> None:
    """An Admin reads every meeting's notifications, shared with them or not."""
    if observed.get("test_user_is_admin"):
        pytest.skip(
            "the Cognito test user is in the Admin group, which reads every meeting; "
            "the refusal is checked with a test user in the User group"
        )


def test_a_user_the_meeting_is_not_shared_with_is_refused(observed) -> None:
    _skip_for_an_admin(observed)
    assert observed["read_before_share"] is None, "read before the share was answered"


def test_a_later_share_sees_the_earlier_notifications(observed) -> None:
    assert "success" in str(observed["share_result"]).lower(), observed["share_result"]
    assert observed["read_after_share_ids"] == [observed["guidance_id"], observed["status_id"]]
    guidance = observed["read_after_share"][0]
    assert guidance["Type"] == "ask_question"
    assert guidance["Refs"] == ["checklist:c1", "segment:seg-1"]
    assert guidance["DedupKey"] == "ask_question:c1"
    assert observed["read_guidance_only"] == ["guidance"]


def test_a_removed_share_stops_seeing_them(observed) -> None:
    _skip_for_an_admin(observed)
    assert observed["read_after_unshare"] is None, "read after the share was removed was answered"
    assert observed["state_after_unshare"] is None, "the user's view was changed after the share was removed"


# ── live delivery ───────────────────────────────────────────────────────────

def _top_level_fields(document: str) -> set:
    """The fields a subscription document selects on its one root field."""
    body = document[document.index("{", document.index("{") + 1) + 1 :]
    fields, depth = set(), 0
    for token in re.findall(r"[A-Za-z_]\w*|[{}]", body):
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth < 0:
                break
        elif depth == 0:
            fields.add(token)
    return fields


def test_the_ui_subscription_fields_are_read_from_the_ui() -> None:
    fields = _top_level_fields(ui_subscription())
    assert {"CallId", "NotificationId", "Actions", "Owner", "SharedWith"} <= fields, fields
    assert "Label" not in fields, "a nested field was read as a top-level one"


def _delivered(live: dict, call_id: str) -> dict:
    """The published notification as the meeting subscription received it, after checking both."""
    if "skipped" in live:
        pytest.skip(live["skipped"])
    assert "error" not in live, live["error"]
    assert live["acks"] == {"meeting": "start_ack", "every": "start_ack"}, live["acks"]
    for name in ("meeting", "every"):
        received = live["received"][name]
        assert received, f"the {name} subscription received nothing: {live}"
        (notification,) = [item for item in received if item["NotificationId"] == live["published_id"]]
        assert notification["CallId"] == call_id
        assert set(notification) == _top_level_fields(ui_subscription())
        assert notification["Owner"], "the delivered message carries no Owner"
        if name == "meeting":
            delivered = notification
    return delivered


def test_a_publish_reaches_the_ui_subscriptions_of_a_user_it_is_shared_with(observed) -> None:
    shared = _delivered(observed["live_shared"], observed["call_id"])
    assert shared["SharedWith"], "the delivered message carries no SharedWith"


def test_a_publish_reaches_the_ui_subscriptions_of_the_owner(observed) -> None:
    _delivered(observed["live_owned"], observed["own_call_id"])


def test_the_web_ui_shows_a_notification_as_it_arrives(observed) -> None:
    page = observed["page"]
    if "skipped" in page:
        pytest.skip(page["skipped"])
    assert "error" not in page, page
    # At least the one published; another test may publish to the test user's meetings meanwhile.
    assert re.search(r"\b[1-9]\d* during this visit", page["bell_on_list"] or ""), page
    # The meeting had none, so its page offered no Notifications drawer until the publish.
    assert page["drawer_triggers_before"] == [], page
    assert page["drawer_opened"], f"the meeting's first notification did not open its drawer: {page}"


# ── dedup and the user's view ───────────────────────────────────────────────


def test_a_second_publish_with_the_dedup_key_updates_the_first(observed) -> None:
    assert observed["dedup_created"] is False
    assert observed["dedup_id"] == observed["guidance_id"]
    assert observed["dedup_revision"] == 2
    (guidance,) = observed["guidance_after_dedup"]
    assert guidance["Severity"] == "WARNING" and guidance["Revision"] == 2
    # Published after the share, so the message now reaches the test user too.
    assert observed["dedup_shared_with"], "the update was published without the new share"


def test_dismissals_and_mutes_are_kept_for_the_user(observed) -> None:
    assert observed["state"]["Dismissed"] == [observed["guidance_id"]]
    assert observed["state"]["MutedCategories"] == ["status"]
    assert "integ-probe" in (observed["prefs_muted"] or [])
    assert "integ-probe" not in (observed["prefs_restored"] or [])


# ── limits and retention ────────────────────────────────────────────────────


def test_a_meeting_takes_120_publishes_a_minute(observed) -> None:
    if observed["burst_seconds"] > 55:
        pytest.skip(f"the burst took {observed['burst_seconds']:.0f}s and may have crossed a minute")
    assert observed["burst_accepted"] == 120, observed["burst_accepted"]
    assert observed["burst_refused"], "the 121st publish in the minute was accepted"


def test_the_transcript_deletion_removes_the_guidance_only(observed) -> None:
    assert observed["deletion_error"] is None
    assert observed["categories_after_transcript_deletion"] == ["status"]
    assert observed["user_rows_after_transcript_deletion"] == 1


def test_guidance_is_refused_once_the_transcript_is_deleted(observed) -> None:
    assert observed["guidance_after_deletion"] == "refused", observed["guidance_after_deletion"]


def test_deleting_the_meeting_deletes_every_notification_row(observed) -> None:
    assert not observed["meeting_after_delete"], observed["delete_result"]
    assert observed["rows_after_delete"] == 0
