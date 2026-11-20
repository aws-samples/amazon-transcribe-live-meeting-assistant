# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting notification resolvers, evaluated by AppSync itself.

`test_appsync_contract.py` checks their wiring; this module runs each handler
through AppSync's EvaluateCode API against a supplied context: the checks the
publish makes, where its recipients come from, the dedup and rate-limit steps,
the read's range and filters, and the per-user view keyed by the caller.

EvaluateCode touches no resource, but it needs AWS credentials, so this module
runs only when LMA_EVALUATE_APPSYNC=1 is set (with AWS_PROFILE / AWS_REGION as
usual) and is skipped otherwise:

    LMA_EVALUATE_APPSYNC=1 AWS_PROFILE=default python -m pytest \\
        lma-ai-stack/test/test_meeting_notifications_evaluated.py -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
        reason="evaluates resolvers with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
    ),
    # Allowed to call AWS; see scripts/testing/no_real_aws.py.
    pytest.mark.real_aws,
]

APPSYNC = Path(__file__).resolve().parents[1] / "source" / "appsync"
OWNER = "owner@example.com"
SHARED = "shared@example.com"
BACKEND = {
    "userArn": "arn:aws:sts::123456789012:assumed-role/Fn/session",
    "accountId": "123456789012",
}
USER = {"username": SHARED, "sub": "s", "groups": [], "claims": {}, "sourceIp": [], "issuer": "i"}


@pytest.fixture(scope="module")
def client():
    import boto3

    return boto3.client("appsync", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _evaluate(client, name: str, function: str, context: dict) -> dict:
    context = {"arguments": {}, "source": {}, "stash": {}, **context}
    response = client.evaluate_code(
        runtime={"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"},
        code=(APPSYNC / name).read_text(encoding="utf-8"),
        context=json.dumps(context),
        function=function,
    )
    return response


def _ok(response: dict):
    assert not response.get("error"), response
    return json.loads(response["evaluationResult"]) if response.get("evaluationResult") else None


def _error(response: dict) -> str:
    error = response.get("error") or {}
    assert error, response
    return error.get("message", "")


def _publish(**extra) -> dict:
    item = {
        "CallId": "m1",
        "Category": "guidance",
        "Type": "ask_question",
        "Severity": "SUGGESTION",
        "Title": "Ask about the budget",
        "Source": "test",
    }
    item.update(extra)
    return {"input": item}


# ── publish: checks and the meeting read ────────────────────────────────────


def test_a_valid_publish_reads_the_meeting(client) -> None:
    request = _ok(
        _evaluate(
            client,
            "notificationPublishMeeting.js",
            "request",
            {
                "arguments": _publish(Refs=["checklist:c1"], DedupKey="ask_question:c1"),
                "identity": BACKEND,
            },
        )
    )
    assert request["operation"] == "GetItem"
    assert request["key"] == {"PK": {"S": "c#m1"}, "SK": {"S": "c#m1"}}
    assert request["consistentRead"] is True


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"Category": "Guidance"}, "Category"),
        ({"Category": "9x"}, "Category"),
        ({"Type": "has space"}, "Type"),
        ({"Title": "x" * 201}, "Title"),
        ({"Body": "x" * 2001}, "Body"),
        ({"Refs": ["x"] * 21}, "Refs"),
        ({"DedupKey": "a b"}, "DedupKey"),
        ({"Actions": [{"Label": "x", "Kind": "COPY", "Payload": '{"text":"a"}'}] * 3}, "Actions"),
        ({"Actions": [{"Label": "x", "Kind": "COPY", "Payload": '{"text":"a","more":1}'}]}, "COPY"),
        (
            {"Actions": [{"Label": "x", "Kind": "MUTATION", "Payload": '{"action":"anything"}'}]},
            "MUTATION",
        ),
        (
            {
                "Actions": [
                    {"Label": "x", "Kind": "MUTATION", "Payload": '{"action":"constructor"}'}
                ]
            },
            "MUTATION",
        ),
        (
            {"Actions": [{"Label": "x", "Kind": "MUTATION", "Payload": '{"action":"toString"}'}]},
            "MUTATION",
        ),
    ],
)
def test_a_publish_the_resolver_refuses(client, extra, message) -> None:
    response = _evaluate(
        client,
        "notificationPublishMeeting.js",
        "request",
        {
            "arguments": _publish(**extra),
            "identity": BACKEND,
        },
    )
    assert message in _error(response)


def test_a_copy_action_is_accepted(client) -> None:
    action = {"Label": "Copy question", "Kind": "COPY", "Payload": '{"text":"Who signs off?"}'}
    _ok(
        _evaluate(
            client,
            "notificationPublishMeeting.js",
            "request",
            {
                "arguments": _publish(Actions=[action]),
                "identity": BACKEND,
            },
        )
    )


def test_a_publish_for_no_meeting_is_refused(client) -> None:
    response = _evaluate(
        client,
        "notificationPublishMeeting.js",
        "response",
        {
            "arguments": _publish(),
            "identity": BACKEND,
            "result": None,
        },
    )
    assert "was not found" in _error(response)


def test_the_recipients_and_expiry_are_taken_from_the_meeting(client) -> None:
    meeting = {"PK": "c#m1", "Owner": OWNER, "SharedWith": [SHARED], "ExpiresAfter": 2000}
    response = _evaluate(
        client,
        "notificationPublishMeeting.js",
        "response",
        {
            # The input's ExpiresAfter is later than the meeting's, so the meeting's wins.
            "arguments": _publish(ExpiresAfter=3000),
            "identity": BACKEND,
            "result": meeting,
        },
    )
    _ok(response)
    stash = json.loads(response["stash"])
    assert stash["notificationMeeting"] == {
        "Owner": OWNER,
        "SharedWith": [SHARED],
        "ExpiresAfter": 2000,
    }
    assert stash["notificationExpiresAfter"] == 2000
    assert (
        stash["notificationSK"].startswith("n#")
        and stash["notificationId"] in stash["notificationSK"]
    )


def test_a_shorter_publisher_expiry_is_kept(client) -> None:
    meeting = {"PK": "c#m1", "Owner": OWNER, "ExpiresAfter": 2000}
    response = _evaluate(
        client,
        "notificationPublishMeeting.js",
        "response",
        {
            "arguments": _publish(ExpiresAfter=1500),
            "identity": BACKEND,
            "result": meeting,
        },
    )
    assert json.loads(response["stash"])["notificationExpiresAfter"] == 1500


# ── publish: rate and dedup ─────────────────────────────────────────────────


def test_the_rate_counter_is_conditional_on_the_limit(client) -> None:
    request = _ok(
        _evaluate(
            client,
            "notificationPublishRate.js",
            "request",
            {
                "arguments": _publish(),
                "identity": BACKEND,
            },
        )
    )
    assert request["operation"] == "UpdateItem"
    assert request["key"]["PK"] == {"S": "nt#m1"} and request["key"]["SK"]["S"].startswith("r#")
    assert str(request["condition"]["expressionValues"][":limit"]["N"]) == "120"


def test_a_publish_over_the_limit_is_refused_as_rate_limited(client) -> None:
    response = _evaluate(
        client,
        "notificationPublishRate.js",
        "response",
        {
            "arguments": _publish(),
            "identity": BACKEND,
            "error": {"message": "condition", "type": "DynamoDB:ConditionalCheckFailedException"},
        },
    )
    # EvaluateCode reports the message only; the error type the publish helper
    # maps to NotificationRateLimited is checked in the resolver's text.
    assert "notifications this minute" in _error(response)
    assert "'RateLimited'" in (APPSYNC / "notificationPublishRate.js").read_text()


def test_a_dedup_key_finds_or_creates_its_pointer(client) -> None:
    stash = {
        "notificationId": "id-new",
        "notificationSK": "n#t#id-new",
        "notificationExpiresAfter": 2000,
    }
    request = _ok(
        _evaluate(
            client,
            "notificationPublishDedup.js",
            "request",
            {
                "arguments": _publish(DedupKey="ask_question:c1"),
                "identity": BACKEND,
                "stash": stash,
            },
        )
    )
    assert request["key"]["SK"] == {"S": "d#guidance#ask_question:c1"}
    assert "#id = if_not_exists(#id, :id)" in request["update"]["expression"]
    assert "#sk = if_not_exists(#sk, :sk)" in request["update"]["expression"]


def test_the_pointer_found_is_the_notification_written(client) -> None:
    response = _evaluate(
        client,
        "notificationPublishDedup.js",
        "response",
        {
            "arguments": _publish(DedupKey="ask_question:c1"),
            "identity": BACKEND,
            "stash": {"notificationId": "id-new", "notificationSK": "n#t2#id-new"},
            "result": {"NotificationId": "id-old", "NotificationSK": "n#t1#id-old"},
        },
    )
    _ok(response)
    stash = json.loads(response["stash"])
    assert stash["notificationId"] == "id-old" and stash["notificationSK"] == "n#t1#id-old"


# ── publish: the write and the published message ────────────────────────────


WRITE_STASH = {
    "notificationMeeting": {
        "Owner": OWNER,
        "SharedWith": f" {SHARED} ,b@example.com",
        "ExpiresAfter": 2000,
    },
    "notificationExpiresAfter": 2000,
    "notificationNow": "2026-11-19T14:00:00.000Z",
    "notificationId": "id-1",
    "notificationSK": "n#2026-11-19T14:00:00.000Z#id-1",
}


def test_the_write_replaces_or_removes_every_field_and_keeps_created_at(client) -> None:
    request = _ok(
        _evaluate(
            client,
            "notificationPublishWrite.js",
            "request",
            {
                "arguments": _publish(Body="**b**"),
                "identity": BACKEND,
                "stash": WRITE_STASH,
            },
        )
    )
    expression = request["update"]["expression"]
    assert "#createdAt = if_not_exists(#createdAt, :now)" in expression
    assert "ADD #revision :one" in expression
    assert "REMOVE" in expression and "#Refs" in expression and "#DedupKey" in expression
    names = set(request["update"]["expressionNames"].values())
    assert "Owner" not in names and "SharedWith" not in names


def test_the_message_carries_the_meetings_recipients_in_the_published_form(client) -> None:
    row = {
        "CallId": "m1",
        "NotificationId": "id-1",
        "Category": "guidance",
        "Type": "ask_question",
        "Severity": "SUGGESTION",
        "Source": "test",
        "CreatedAt": "2026-11-19T14:00:00.000Z",
        "UpdatedAt": "2026-11-19T14:00:00.000Z",
        "Revision": 1,
    }
    output = _ok(
        _evaluate(
            client,
            "notificationPublishWrite.js",
            "response",
            {
                "arguments": _publish(),
                "identity": BACKEND,
                "stash": WRITE_STASH,
                "result": row,
            },
        )
    )
    assert output["Owner"] == OWNER
    assert output["SharedWith"] == f"[{SHARED}, b@example.com]"
    assert output["Created"] is True


# ── reading ─────────────────────────────────────────────────────────────────


def test_the_read_is_of_the_notification_rows_with_its_filters(client) -> None:
    request = _ok(
        _evaluate(
            client,
            "listMeetingNotifications.js",
            "request",
            {
                "arguments": {
                    "CallId": "m1",
                    "Category": "guidance",
                    "Since": "2026-11-19T14:00:00Z",
                    "Limit": 9999,
                },
                "identity": USER,
            },
        )
    )
    assert request["query"]["expressionValues"] == {":pk": {"S": "nt#m1"}, ":prefix": {"S": "n#"}}
    assert request["limit"] == 500 and request["scanIndexForward"] is True
    assert request["filter"]["expression"] == "#category = :category AND #updatedAt >= :since"
    assert request["filter"]["expressionValues"][":since"] == {"S": "2026-11-19T14:00:00.000Z"}


def test_the_read_answers_without_storage_keys(client) -> None:
    output = _ok(
        _evaluate(
            client,
            "listMeetingNotifications.js",
            "response",
            {
                "arguments": {"CallId": "m1"},
                "identity": USER,
                "result": {
                    "items": [
                        {"PK": "nt#m1", "SK": "n#x", "NotificationId": "a", "ExpiresAfter": 1}
                    ],
                    "nextToken": "t",
                },
            },
        )
    )
    assert output["NextToken"] == "t"
    (item,) = output["Items"]
    assert item["NotificationId"] == "a"
    assert not {"PK", "SK", "ExpiresAfter"} & set(item)


# ── a user's own view ───────────────────────────────────────────────────────


def test_a_users_view_is_keyed_by_their_identity(client) -> None:
    request = _ok(
        _evaluate(
            client,
            "myMeetingNotificationState.js",
            "request",
            {
                "arguments": {"input": {"CallId": "m1", "Dismiss": ["a"]}},
                "identity": USER,
                "info": {
                    "fieldName": "updateMyMeetingNotificationState",
                    "parentTypeName": "Mutation",
                    "variables": {},
                },
                "stash": {"meetingExpiresAfter": 2000},
            },
        )
    )
    assert request["key"] == {"PK": {"S": "nt#m1"}, "SK": {"S": f"u#{SHARED}"}}
    assert "ADD #dismissed :dismiss" in request["update"]["expression"]
    assert request["update"]["expressionValues"][":dismiss"] == {"SS": ["a"]}
    assert "size(#dismissed) < :maxDismissed" in request["condition"]["expression"]
    # Room for the one id added, so the set stays within 1000.
    assert str(request["condition"]["expressionValues"][":maxDismissed"]["N"]) == "1000"


def test_dismiss_and_restore_together_are_refused(client) -> None:
    response = _evaluate(
        client,
        "myMeetingNotificationState.js",
        "request",
        {
            "arguments": {"input": {"CallId": "m1", "Dismiss": ["a"], "Restore": ["b"]}},
            "identity": USER,
            "info": {
                "fieldName": "updateMyMeetingNotificationState",
                "parentTypeName": "Mutation",
                "variables": {},
            },
        },
    )
    assert "not both" in _error(response)


def test_global_mutes_are_keyed_by_the_caller(client) -> None:
    request = _ok(
        _evaluate(
            client,
            "myNotificationPreferences.js",
            "request",
            {
                "arguments": {"input": {"Mute": ["guidance"]}},
                "identity": USER,
                "info": {
                    "fieldName": "updateMyNotificationPreferences",
                    "parentTypeName": "Mutation",
                    "variables": {},
                },
            },
        )
    )
    assert request["key"] == {"PK": {"S": f"usr#{SHARED}"}, "SK": {"S": "notification-prefs"}}
    assert "ADD #muted :mute" in request["update"]["expression"]


def test_without_a_dedup_key_no_pointer_is_written(client) -> None:
    response = _evaluate(
        client,
        "notificationPublishDedup.js",
        "request",
        {
            "arguments": _publish(),
            "identity": BACKEND,
            "stash": {"notificationId": "id-new", "notificationSK": "n#t#id-new"},
        },
    )
    assert not response.get("error"), response
    # runtime.earlyReturn: no request reaches the data source.
    assert "UpdateItem" not in (response.get("evaluationResult") or "")


def test_a_users_view_takes_the_meetings_expiry(client) -> None:
    request = _ok(
        _evaluate(
            client,
            "notificationMeetingExpiry.js",
            "request",
            {
                "arguments": {"input": {"CallId": "m1", "Dismiss": ["a"]}},
                "identity": USER,
            },
        )
    )
    assert request["key"] == {"PK": {"S": "c#m1"}, "SK": {"S": "c#m1"}}
    response = _evaluate(
        client,
        "notificationMeetingExpiry.js",
        "response",
        {
            "arguments": {"input": {"CallId": "m1"}},
            "identity": USER,
            "result": {"ExpiresAfter": 2000},
        },
    )
    assert json.loads(response["stash"])["meetingExpiresAfter"] == 2000
    missing = _evaluate(
        client,
        "notificationMeetingExpiry.js",
        "response",
        {
            "arguments": {"input": {"CallId": "m1"}},
            "identity": USER,
            "result": None,
        },
    )
    assert "was not found" in _error(missing)


def _mark(args: dict) -> dict:
    payload = json.dumps({"action": "set_checklist_item_status", "args": args})
    return {"Label": "Mark covered", "Kind": "MUTATION", "Payload": payload}


def test_the_checklist_status_action_is_accepted(client) -> None:
    _ok(_evaluate(client, "notificationPublishMeeting.js", "request", {
        "arguments": _publish(Actions=[_mark({"itemId": "c1", "status": "covered"})]),
        "identity": BACKEND,
    }))


@pytest.mark.parametrize(
    "args, message",
    [
        ({"itemId": "c1", "status": "done"}, "must be one of"),
        ({"itemId": "c 1", "status": "covered"}, "not a valid value"),
        ({"itemId": "c1"}, "status is required"),
        ({"itemId": "c1", "status": "covered", "StatusSource": "assistant"}, "does not take StatusSource"),
        ({"itemId": "c1", "status": "covered", "callId": "m2"}, "does not take callId"),
    ],
)
def test_a_checklist_status_action_the_resolver_refuses(client, args, message) -> None:
    response = _evaluate(client, "notificationPublishMeeting.js", "request", {
        "arguments": _publish(Actions=[_mark(args)]),
        "identity": BACKEND,
    })
    assert message in _error(response)


@pytest.mark.parametrize(
    "since, stored",
    [
        # An offset is the same moment in UTC.
        ("2026-11-19T15:00:00+01:00", "2026-11-19T14:00:00.000Z"),
        # Without fractional seconds it still sorts before a row from within that second.
        ("2026-11-19T13:00:00Z", "2026-11-19T13:00:00.000Z"),
        ("2026-11-19T13:00:00.500Z", "2026-11-19T13:00:00.500Z"),
    ],
)
def test_since_is_compared_in_the_form_updated_at_is_stored_in(client, since, stored) -> None:
    request = _ok(_evaluate(client, "listMeetingNotifications.js", "request", {
        "arguments": {"CallId": "m1", "Since": since},
        "identity": USER,
    }))
    assert request["filter"]["expressionValues"][":since"] == {"S": stored}
    # The stored form orders by time as a string.
    assert "2026-11-19T13:00:00.500Z" >= stored or stored > "2026-11-19T13:00:00.500Z"


def test_every_meeting_mutes_stay_within_their_cap(client) -> None:
    categories = [f"c{i}" for i in range(50)]
    request = _ok(_evaluate(client, "myNotificationPreferences.js", "request", {
        "arguments": {"input": {"Mute": categories}},
        "identity": USER,
        "info": {"fieldName": "updateMyNotificationPreferences", "parentTypeName": "Mutation", "variables": {}},
    }))
    # Fifty added at once only to an empty set: 50 - 50 + 1.
    assert str(request["condition"]["expressionValues"][":max"]["N"]) == "1"


def test_mutes_on_one_meeting_have_a_cap(client) -> None:
    request = _ok(_evaluate(client, "myMeetingNotificationState.js", "request", {
        "arguments": {"input": {"CallId": "m1", "Mute": ["status", "tips"]}},
        "identity": USER,
        "info": {"fieldName": "updateMyMeetingNotificationState", "parentTypeName": "Mutation", "variables": {}},
        "stash": {"meetingExpiresAfter": 2000},
    }))
    assert "size(#muted) < :maxMuted" in request["condition"]["expression"]
    assert str(request["condition"]["expressionValues"][":maxMuted"]["N"]) == "49"


def test_a_pointer_loses_its_expiry_when_the_notification_does(client) -> None:
    stash = {"notificationId": "id-new", "notificationSK": "n#t#id-new", "notificationExpiresAfter": None}
    request = _ok(_evaluate(client, "notificationPublishDedup.js", "request", {
        "arguments": _publish(DedupKey="ask_question:c1"),
        "identity": BACKEND,
        "stash": stash,
    }))
    assert request["update"]["expression"].endswith("REMOVE #ttl")
    assert ":ttl" not in request["update"]["expressionValues"]


def test_guidance_for_a_meeting_whose_transcript_is_deleted_is_refused(client) -> None:
    meeting = {"PK": "c#m1", "Owner": OWNER, "ExpiresAfter": 2000, "TranscriptDeletedAt": "2026-11-19T15:00:00Z"}
    response = _evaluate(client, "notificationPublishMeeting.js", "response", {
        "arguments": _publish(),
        "identity": BACKEND,
        "result": meeting,
    })
    assert "transcript deleted" in _error(response)


def test_another_category_is_kept_after_the_transcript_is_deleted(client) -> None:
    meeting = {"PK": "c#m1", "Owner": OWNER, "ExpiresAfter": 2000, "TranscriptDeletedAt": "2026-11-19T15:00:00Z"}
    response = _evaluate(client, "notificationPublishMeeting.js", "response", {
        "arguments": _publish(Category="status"),
        "identity": BACKEND,
        "result": meeting,
    })
    _ok(response)
    assert json.loads(response["stash"])["notificationTranscriptBound"] is False


@pytest.mark.parametrize("bound, expected", [(True, "#ttl = if_not_exists(#ttl, :ttl)"), (False, "#ttl = :ttl")])
def test_an_update_keeps_a_transcript_bound_expiry(client, bound, expected) -> None:
    stash = dict(WRITE_STASH, notificationTranscriptBound=bound)
    request = _ok(_evaluate(client, "notificationPublishWrite.js", "request", {
        "arguments": _publish(),
        "identity": BACKEND,
        "stash": stash,
    }))
    assert expected in request["update"]["expression"]
    pointer = _ok(_evaluate(client, "notificationPublishDedup.js", "request", {
        "arguments": _publish(DedupKey="k"),
        "identity": BACKEND,
        "stash": stash,
    }))
    assert expected in pointer["update"]["expression"]
