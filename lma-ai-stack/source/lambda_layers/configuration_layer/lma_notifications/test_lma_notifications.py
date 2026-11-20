# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""publish_meeting_notification: the input it builds, its checks, and its errors."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import lma_notifications as notifications  # noqa: E402


class FakeApi:
    """Records each request and answers with the input echoed back."""

    def __init__(self, revision: int = 1):
        self.requests = []
        self.revision = revision

    def __call__(self, query, variables):
        self.requests.append((query, variables))
        item = dict(variables["input"])
        item.update(
            NotificationId="n-1",
            Revision=self.revision,
            Created=self.revision == 1,
            Owner="owner@example.com",
            SharedWith="[a@example.com]",
        )
        return {"publishMeetingNotification": item}


@pytest.fixture(autouse=True)
def _no_transcript_retention(monkeypatch):
    monkeypatch.setattr(notifications, "transcript_expires_after", lambda now=None: None)


def _publish(api, **overrides):
    arguments = {
        "category": "status",
        "type": "note",
        "title": "A title",
        "source": "test",
        "graphql": api,
    }
    arguments.update(overrides)
    return notifications.publish_meeting_notification("call-1", **arguments)


def test_publishes_the_mutation_with_the_given_fields():
    api = FakeApi()
    stored = _publish(
        api,
        body="**Body**",
        severity="suggestion",
        refs=["checklist:item-1", "segment:seg-9"],
        dedup_key="ask_question:item-1",
        expires_at="2026-11-19T15:00:00Z",
        created_by="user@example.com",
    )
    query, variables = api.requests[0]
    assert "mutation PublishMeetingNotification" in query
    assert variables["input"] == {
        "CallId": "call-1",
        "Category": "status",
        "Type": "note",
        "Severity": "SUGGESTION",
        "Title": "A title",
        "Source": "test",
        "Body": "**Body**",
        "Refs": ["checklist:item-1", "segment:seg-9"],
        "DedupKey": "ask_question:item-1",
        "ExpiresAt": "2026-11-19T15:00:00Z",
        "CreatedBy": "user@example.com",
    }
    assert stored["Created"] is True


def test_the_published_message_selects_owner_and_shared_with():
    """subscription.js filters each message on both, so the mutation must select them."""
    selection = notifications.PUBLISH_MEETING_NOTIFICATION
    assert "\n    Owner\n" in selection and "\n    SharedWith\n" in selection
    assert "\n    CallId\n" in selection


def test_no_recipient_is_sent_by_the_publisher():
    api = FakeApi()
    _publish(api)
    sent = api.requests[0][1]["input"]
    assert "Owner" not in sent and "SharedWith" not in sent


def test_refs_are_kept_as_given():
    api = FakeApi()
    refs = ["segment:a b#c", "checklist:Z/9"]
    _publish(api, refs=refs)
    assert api.requests[0][1]["input"]["Refs"] == refs


def test_an_update_through_the_dedup_key_reports_not_created():
    stored = _publish(FakeApi(revision=2), dedup_key="k")
    assert stored["Created"] is False
    assert stored["Revision"] == 2


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"category": "Guidance"}, "category"),
        ({"category": "9x"}, "category"),
        ({"category": "a" * 33}, "category"),
        ({"type": "has space"}, "type"),
        ({"title": "x" * 201}, "title"),
        ({"body": "x" * 2001}, "body"),
        ({"severity": "critical"}, "severity"),
        ({"refs": ["x"] * 21}, "refs"),
        ({"refs": [""]}, "ref"),
        ({"dedup_key": "has space"}, "dedup_key"),
        ({"source": " "}, "source"),
        ({"expires_at": "tomorrow"}, "expires_at"),
    ],
)
def test_refuses_input_the_api_would_refuse(overrides, message):
    api = FakeApi()
    with pytest.raises(ValueError, match=message):
        _publish(api, **overrides)
    assert api.requests == []


def test_a_copy_action_is_sent_as_json():
    api = FakeApi()
    _publish(api, actions=[{"label": "Copy question", "kind": "copy", "payload": {"text": "Why?"}}])
    action = api.requests[0][1]["input"]["Actions"][0]
    assert action == {"Label": "Copy question", "Kind": "COPY", "Payload": '{"text":"Why?"}'}


@pytest.mark.parametrize(
    "actions",
    [
        [{"label": "x", "kind": "copy", "payload": {"text": "a"}}] * 3,
        [{"label": "x", "kind": "run", "payload": {"text": "a"}}],
        [{"label": "x", "kind": "copy", "payload": {"text": "a", "extra": 1}}],
        [{"label": "x", "kind": "copy", "payload": "not json"}],
        [{"label": "x", "kind": "mutation", "payload": {"query": "mutation { x }"}}],
        [{"label": "", "kind": "copy", "payload": {"text": "a"}}],
    ],
)
def test_refuses_malformed_actions(actions):
    with pytest.raises(ValueError):
        _publish(FakeApi(), actions=actions)


def test_guidance_gets_the_transcript_expiry(monkeypatch):
    monkeypatch.setattr(notifications, "transcript_expires_after", lambda now=None: 1_900_000_000)
    api = FakeApi()
    _publish(api, category="guidance")
    assert api.requests[0][1]["input"]["ExpiresAfter"] == 1_900_000_000


def test_other_categories_leave_the_expiry_to_the_meeting(monkeypatch):
    monkeypatch.setattr(notifications, "transcript_expires_after", lambda now=None: 1_900_000_000)
    api = FakeApi()
    _publish(api, category="status")
    assert "ExpiresAfter" not in api.requests[0][1]["input"]


def test_the_transcript_expiry_follows_the_retention(monkeypatch):
    monkeypatch.undo()
    import lma_config

    monkeypatch.setattr(lma_config, "read_setting", lambda *a, **k: 10)
    monkeypatch.setattr(lma_config, "deletion_delay_minutes", lambda **k: None)
    assert notifications.transcript_expires_after(now=1000) == 1000 + 10 * 86400
    monkeypatch.setattr(lma_config, "deletion_delay_minutes", lambda **k: 15)
    limit = lma_config.SEGMENT_EXPIRY_LIMIT_DAYS
    assert notifications.transcript_expires_after(now=1000) == int(1000 + limit * 86400)


def test_an_unreadable_retention_leaves_the_meeting_expiry(monkeypatch):
    monkeypatch.undo()
    import lma_config

    def fail(*_a, **_k):
        raise RuntimeError("no table")

    monkeypatch.setattr(lma_config, "read_setting", fail)
    assert notifications.transcript_expires_after(now=1000) is None


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _answer(monkeypatch, payload):
    """Stand in for the signed HTTPS request with a fixed answer."""
    monkeypatch.setenv(
        "APPSYNC_GRAPHQL_URL", "https://example.appsync-api.us-east-1.amazonaws.com/graphql"
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    sent = []

    def urlopen(request, timeout):
        sent.append(request)
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(notifications.urllib.request, "urlopen", urlopen)
    return sent


def test_the_request_is_signed_for_appsync(monkeypatch):
    sent = _answer(monkeypatch, {"data": {"publishMeetingNotification": {"NotificationId": "n"}}})
    stored = _publish(None)
    assert stored == {"NotificationId": "n"}
    headers = {key.lower(): value for key, value in sent[0].header_items()}
    assert "/appsync/aws4_request" in headers["authorization"]


@pytest.mark.parametrize(
    "error_type, raised",
    [
        ("RateLimited", notifications.NotificationRateLimited),
        ("NotFound", notifications.NotificationMeetingNotFound),
        ("TranscriptDeleted", notifications.NotificationTranscriptDeleted),
        ("BadRequest", notifications.NotificationError),
    ],
)
def test_api_errors_are_raised_by_type(monkeypatch, error_type, raised):
    _answer(monkeypatch, {"errors": [{"message": "no", "errorType": error_type}]})
    with pytest.raises(raised) as caught:
        _publish(None)
    assert caught.value.error_type == error_type


def test_refuses_an_endpoint_that_is_not_https(monkeypatch):
    monkeypatch.setenv("APPSYNC_GRAPHQL_URL", "http://example.com/graphql")
    with pytest.raises(notifications.NotificationError):
        _publish(None)


def test_a_notification_may_leave_the_title_to_the_page():
    api = FakeApi()
    _publish(api, title=None, body="Only a body")
    assert "Title" not in api.requests[0][1]["input"]
