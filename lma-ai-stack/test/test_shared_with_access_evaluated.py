# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The SharedWith access templates, evaluated by AppSync itself.

`test_shared_with_access.py` checks the templates' text; this module runs them,
through AppSync's EvaluateMappingTemplate and EvaluateCode APIs, against every
stored shape of SharedWith and every kind of caller: the meeting's owner, a user
it is shared with, an Admin, a backend role, another user, and users whose name
is part of a listed entry without being one.

Those APIs evaluate a template against a supplied context and touch no
resource, but they need AWS credentials, so this module runs only when
LMA_EVALUATE_APPSYNC=1 is set (with AWS_PROFILE / AWS_REGION as usual) and is
skipped otherwise:

    LMA_EVALUATE_APPSYNC=1 AWS_PROFILE=default python -m pytest \
        lma-ai-stack/test/test_shared_with_access_evaluated.py -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
    reason="evaluates templates with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
)

APPSYNC = Path(__file__).resolve().parents[1] / "source" / "appsync"

OWNER = "owner@example.com"
LISTED = "bob@example.com"
OTHER_LISTED = "alice@example.com"

# Every shape SharedWith reaches a template in. A string set arrives as a list:
# AppSync converts it when it reads the item.
SHAPES = {
    "list": [OTHER_LISTED, LISTED],
    "comma-separated": f"{OTHER_LISTED},{LISTED}",
    "comma-and-space": f"{OTHER_LISTED}, {LISTED}",
    "bracketed": f"[{OTHER_LISTED}, {LISTED}]",
    "bracketed-first": f"[{LISTED}, {OTHER_LISTED}]",
    "bracketed-single": f"[{LISTED}]",
    "single": LISTED,
}

COGNITO = {
    "owner": ({"username": OWNER, "groups": []}, True),
    "listed": ({"username": LISTED, "groups": []}, True),
    "admin": ({"username": "admin@example.com", "groups": ["Admin"]}, True),
    "other": ({"username": "carol@example.com", "groups": []}, False),
    # Part of a listed entry, and in the String shapes part of the stored text.
    "suffix-of-entry": ({"username": "ob@example.com", "groups": []}, False),
    "prefix-of-entry": ({"username": "bob@example.co", "groups": []}, False),
    "spans-two-entries": ({"username": "com, bob", "groups": []}, False),
}
BACKEND = {"userArn": "arn:aws:sts::123456789012:assumed-role/LambdaRole/session"}


@pytest.fixture(scope="module")
def client():
    import boto3  # imported here so the module collects without boto3 when skipped

    return boto3.client("appsync", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _evaluate(client, name: str, context: dict) -> dict:
    response = client.evaluate_mapping_template(
        template=(APPSYNC / name).read_text(encoding="utf-8"), context=json.dumps(context)
    )
    return response


def _allowed(response: dict) -> bool:
    error = response.get("error")
    if error:
        assert "not authorized" in error["message"], error
        return False
    return True


CASES = [
    (shape, caller, identity, expected)
    for shape in SHAPES
    for caller, (identity, expected) in COGNITO.items()
]


@pytest.mark.parametrize("shape,caller,identity,expected", CASES)
def test_get_call_answers_exactly_the_people_the_meeting_is_for(
    client, shape: str, caller: str, identity: dict, expected: bool
) -> None:
    result = {"CallId": "c1", "Owner": OWNER, "SharedWith": SHAPES[shape]}
    response = _evaluate(client, "getCall.response.vtl", {"result": result, "identity": identity})
    assert _allowed(response) is expected
    if expected:
        assert json.loads(response["evaluationResult"])["SharedWith"] == SHAPES[shape]


def test_get_call_answers_a_backend_role(client) -> None:
    result = {"CallId": "c1", "Owner": OWNER, "SharedWith": SHAPES["bracketed"]}
    response = _evaluate(client, "getCall.response.vtl", {"result": result, "identity": BACKEND})
    assert _allowed(response)


@pytest.mark.parametrize("shape,caller,identity,expected", CASES)
def test_transcript_segments_are_answered_to_exactly_the_people_the_meeting_is_for(
    client, shape: str, caller: str, identity: dict, expected: bool
) -> None:
    items = [{"PK": "trs#c1", "SK": "1", "Owner": OWNER, "SharedWith": SHAPES[shape]}]
    response = _evaluate(
        client,
        "getTranscriptSegments.response.vtl",
        {"result": {"items": items, "nextToken": None}, "identity": identity},
    )
    segments = json.loads(response["evaluationResult"])["TranscriptSegments"]
    assert len(segments) == (1 if expected else 0)


@pytest.mark.parametrize("shape,caller,identity,expected", CASES)
def test_a_virtual_participant_is_answered_to_exactly_the_people_it_is_for(
    client, shape: str, caller: str, identity: dict, expected: bool
) -> None:
    row = {"id": "vp1", "Owner": OWNER, "SharedWith": SHAPES[shape]}
    response = _evaluate(
        client, "getVirtualParticipant.response.vtl", {"result": row, "identity": identity}
    )
    assert _allowed(response) is expected
    listed = _evaluate(
        client,
        "listVirtualParticipants.response.vtl",
        {"result": {"items": [row]}, "identity": identity},
    )
    assert len(json.loads(listed["evaluationResult"])) == (1 if expected else 0)


@pytest.mark.parametrize(
    "name,read",
    [
        (
            "getTranscriptSegments.response.vtl",
            lambda output: [s["PK"] for s in json.loads(output)["TranscriptSegments"]],
        ),
        (
            "listVirtualParticipants.response.vtl",
            lambda output: [r["PK"] for r in json.loads(output)],
        ),
    ],
)
def test_each_item_of_a_list_is_judged_on_its_own_shared_with(client, name: str, read) -> None:
    """An item with no SharedWith, or a null one, after an item shared with the
    caller is not answered to them."""
    items = [
        {"PK": "shared", "Owner": OWNER, "SharedWith": [LISTED]},
        {"PK": "absent", "Owner": OWNER},
        {"PK": "null", "Owner": OWNER, "SharedWith": None},
        {"PK": "text", "Owner": OWNER, "SharedWith": SHAPES["bracketed"]},
        {"PK": "absent-again", "Owner": OWNER},
    ]
    response = _evaluate(
        client,
        name,
        {"result": {"items": items, "nextToken": None}, "identity": COGNITO["listed"][0]},
    )
    assert read(response["evaluationResult"]) == ["shared", "text"]


# ── the writes: authorizeCallWrite, then the condition ──────────────────────


def _expected_access(shape: str, caller: str) -> str:
    if caller == "owner":
        return "owner"
    if caller == "listed":
        return "shared-list" if isinstance(SHAPES[shape], list) else "shared-text"
    return "none"


@pytest.mark.parametrize(
    "shape,caller,identity",
    [(shape, caller, identity) for shape, caller, identity, _ in CASES if caller != "admin"],
)
def test_the_read_before_a_write_records_how_the_caller_relates_to_the_meeting(
    client, shape: str, caller: str, identity: dict
) -> None:
    result = {"PK": "c#c1", "SK": "c#c1", "Owner": OWNER, "SharedWith": SHAPES[shape]}
    response = _evaluate(
        client, "authorizeCallWrite.response.vtl", {"result": result, "identity": identity}
    )
    assert not response.get("error"), response
    stash = json.loads(response["stash"])
    assert stash["callerAccess"] == _expected_access(shape, caller)
    if stash["callerAccess"] == "shared-text":
        assert stash["sharedWithAsRead"] == SHAPES[shape]


def test_the_read_before_a_write_finds_no_access_to_a_missing_meeting(client) -> None:
    response = _evaluate(
        client,
        "authorizeCallWrite.response.vtl",
        {"result": None, "identity": COGNITO["listed"][0]},
    )
    assert json.loads(response["stash"])["callerAccess"] == "none"


WRITE_ARGS = {
    "updateSpeakerIdentities.request.vtl": {"CallId": "c1", "SpeakerIdentities": "{}"},
    "updateCallSummaryText.request.vtl": {"CallId": "c1", "CallSummaryText": "text"},
    # A backend role must name where an agenda came from; a signed-in user's
    # source is decided by the template.
    "updateCallAgenda.request.vtl": {"CallId": "c1", "Agenda": "1. Budget", "AgendaSource": "mcp"},
}


def _write_condition(client, name: str, identity: dict, stash: dict) -> dict:
    response = _evaluate(
        client,
        name,
        {"arguments": {"input": WRITE_ARGS[name]}, "identity": identity, "stash": stash},
    )
    assert not response.get("error"), response
    return json.loads(response["evaluationResult"])["condition"]


@pytest.mark.parametrize("name", list(WRITE_ARGS))
def test_each_write_restates_what_the_read_found(client, name: str) -> None:
    listed = COGNITO["listed"][0]
    owner = _write_condition(client, name, COGNITO["owner"][0], {"callerAccess": "owner"})
    assert owner["expression"].startswith("attribute_exists(#PK) AND #Owner = :caller")
    assert owner["expressionValues"][":caller"] == {"S": OWNER}

    as_list = _write_condition(client, name, listed, {"callerAccess": "shared-list"})
    assert (
        "(#Owner = :caller OR ((attribute_type(#SharedWith, :sharedWithList) OR "
        "attribute_type(#SharedWith, :sharedWithSet)) AND contains(#SharedWith, :caller)))"
    ) in as_list["expression"]
    assert as_list["expressionValues"][":sharedWithList"] == {"S": "L"}
    assert as_list["expressionValues"][":sharedWithSet"] == {"S": "SS"}

    as_text = _write_condition(
        client,
        name,
        listed,
        {"callerAccess": "shared-text", "sharedWithAsRead": SHAPES["bracketed"]},
    )
    assert "(#Owner = :caller OR #SharedWith = :sharedWithAsRead)" in as_text["expression"]
    assert as_text["expressionValues"][":sharedWithAsRead"] == {"S": SHAPES["bracketed"]}

    stranger = _write_condition(client, name, COGNITO["other"][0], {"callerAccess": "none"})
    assert "AND #Owner = :caller" in stranger["expression"]
    assert "#SharedWith" not in stranger["expression"]

    for identity in (COGNITO["admin"][0], BACKEND):
        unrestricted = _write_condition(client, name, identity, {"callerAccess": "none"})
        assert "#Owner" not in unrestricted["expression"]
        assert "#SharedWith" not in unrestricted["expression"]


# ── what subscriptions are sent ─────────────────────────────────────────────


PUBLISHED = f"[{OTHER_LISTED}, {LISTED}]"


@pytest.mark.parametrize("shape", ["list", "comma-separated", "comma-and-space", "bracketed"])
@pytest.mark.parametrize(
    "name",
    [
        "dDbPutCondition.response.vtl",
        "addVideoKeyframe.response.vtl",
        "updateVirtualParticipant.response.vtl",
    ],
)
def test_a_published_message_carries_shared_with_as_a_bracketed_list(
    client, name: str, shape: str
) -> None:
    result = {"PK": "c#c1", "Owner": OWNER, "SharedWith": SHAPES[shape], "Status": "STARTED"}
    response = _evaluate(client, name, {"result": result, "identity": BACKEND})
    output = json.loads(response["evaluationResult"])
    assert output["SharedWith"] == PUBLISHED
    assert output["Status"] == "STARTED" and output["Owner"] == OWNER


@pytest.mark.parametrize("value", [None, ""])
def test_a_published_message_with_no_entries_keeps_its_value(client, value) -> None:
    result = {"PK": "c#c1", "Owner": OWNER, "SharedWith": value}
    response = _evaluate(
        client, "dDbPutCondition.response.vtl", {"result": result, "identity": BACKEND}
    )
    assert json.loads(response["evaluationResult"]).get("SharedWith") == value


def test_a_deleted_meeting_is_published_with_a_bracketed_list(client) -> None:
    response = _evaluate(
        client,
        "deleteCall.response.vtl",
        {
            "arguments": {
                "input": {"CallId": "c1", "Owner": OWNER, "SharedWith": SHAPES["comma-separated"]}
            },
            "result": {},
            "identity": BACKEND,
        },
    )
    assert json.loads(response["evaluationResult"]) == {
        "CallId": "c1",
        "Owner": OWNER,
        "SharedWith": PUBLISHED,
    }


def _evaluate_js(client, name: str, context: dict) -> dict:
    response = client.evaluate_code(
        runtime={"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"},
        code=(APPSYNC / name).read_text(encoding="utf-8"),
        context=json.dumps(context),
        function="response",
    )
    assert not response.get("error"), response
    return json.loads(response["evaluationResult"])


def test_shared_meetings_are_published_with_a_bracketed_list(client) -> None:
    result = {"Calls": ["c1"], "Owner": OWNER, "SharedWith": f" {OTHER_LISTED} ,{LISTED}"}
    output = _evaluate_js(client, "shareMeetings.js", {"result": result, "arguments": {}})
    assert output["SharedWith"] == PUBLISHED
    assert output["Calls"] == ["c1"]


def test_an_ended_virtual_participant_is_published_with_a_bracketed_list(client) -> None:
    body = {"id": "vp1", "Owner": OWNER, "SharedWith": SHAPES["comma-separated"], "status": "ENDED"}
    output = _evaluate_js(
        client,
        "endVirtualParticipant.js",
        {"result": {"statusCode": 200, "body": body}, "arguments": {}},
    )
    assert output["SharedWith"] == PUBLISHED
    assert output["status"] == "ENDED"


def test_an_unshared_virtual_participant_keeps_its_empty_value(client) -> None:
    body = {"id": "vp1", "Owner": OWNER, "SharedWith": ""}
    output = _evaluate_js(
        client,
        "endVirtualParticipant.js",
        {"result": {"statusCode": 200, "body": body}, "arguments": {}},
    )
    assert output["SharedWith"] == ""


# ── transcript segments with sentiment ──────────────────────────────────────


@pytest.mark.parametrize("shape,caller,identity,expected", CASES)
def test_segments_with_sentiment_are_answered_to_exactly_the_people_the_meeting_is_for(
    client, shape: str, caller: str, identity: dict, expected: bool
) -> None:
    items = [{"PK": "trs#c1", "SK": "1", "Owner": OWNER, "SharedWith": SHAPES[shape]}]
    response = _evaluate(
        client,
        "getTranscriptSegmentsWithSentiment.response.vtl",
        {"result": {"items": items, "nextToken": None}, "identity": identity},
    )
    segments = json.loads(response["evaluationResult"])["TranscriptSegmentsWithSentiment"]
    assert len(segments) == (1 if expected else 0)


def test_segments_with_sentiment_are_answered_to_a_backend_role(client) -> None:
    items = [{"PK": "trs#c1", "SK": "1", "Owner": OWNER}]
    response = _evaluate(
        client,
        "getTranscriptSegmentsWithSentiment.response.vtl",
        {"result": {"items": items, "nextToken": None}, "identity": BACKEND},
    )
    assert len(json.loads(response["evaluationResult"])["TranscriptSegmentsWithSentiment"]) == 1


# ── "[", "]" and "," only separate entries ──────────────────────────────────

# Values whose text holds the listed user's name next to separators, and
# whether the listed user is an entry of them.
SEPARATOR_SHAPES = [
    ([f"{OTHER_LISTED}, {LISTED}"], False),
    ([f"[{LISTED}]"], False),
    ([f"{LISTED}]", OTHER_LISTED], False),
    ([f"x], {LISTED}", LISTED], True),
    (f"[x], {LISTED}]", True),
    (f"{OTHER_LISTED}][{LISTED}", True),
    (f"[[{LISTED}]]", True),
]


@pytest.mark.parametrize("value,expected", SEPARATOR_SHAPES)
def test_an_entry_never_holds_a_separator(client, value, expected: bool) -> None:
    """getCall, the published form and the subscription filter agree on who is
    an entry, however the separators fall."""
    listed = COGNITO["listed"][0]
    result = {"CallId": "c1", "Owner": OWNER, "SharedWith": value}
    response = _evaluate(client, "getCall.response.vtl", {"result": result, "identity": listed})
    assert _allowed(response) is expected
    published = json.loads(
        _evaluate(client, "dDbPutCondition.response.vtl", {"result": result, "identity": BACKEND})[
            "evaluationResult"
        ]
    )["SharedWith"]
    published_js = _evaluate_js(
        client, "shareMeetings.js", {"result": {"SharedWith": value}, "arguments": {}}
    )["SharedWith"]
    assert published_js == published
    rules = _subscription_filter(client, listed)
    assert _filter_matches(rules, {"Owner": OWNER, "SharedWith": published}) is expected


# ── what subscription.js filters on ─────────────────────────────────────────


def _subscription_filter(client, identity: dict):
    """The filter subscription.js sets for this caller, read from the line it
    logs; None when it sets none."""
    response = client.evaluate_code(
        runtime={"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"},
        code=(APPSYNC / "subscription.js").read_text(encoding="utf-8"),
        context=json.dumps(
            {
                "identity": identity,
                "info": {
                    "fieldName": "onUpdateCall",
                    "parentTypeName": "Subscription",
                    "selectionSetGraphQL": "",
                    "selectionSetList": ["Owner", "SharedWith"],
                    "variables": {},
                },
            }
        ),
        function="response",
    )
    assert not response.get("error"), response
    marker = "subscription filter: "
    for line in response.get("logs", []):
        if marker in line:
            logged = json.loads(line[line.index('"', line.index(": ")) :])
            return json.loads(logged[len(marker) :])
    return None


def _filter_matches(rules: dict, message: dict) -> bool:
    """AppSync's string operators: eq is equality, beginsWith a prefix test and
    contains a substring test."""
    for rule in rules["or"]:
        ((field, condition),) = rule.items()
        ((op, operand),) = condition.items()
        value = message.get(field)
        if not isinstance(value, str):
            continue
        if op == "eq" and value == operand:
            return True
        if op == "beginsWith" and value.startswith(operand):
            return True
        if op == "contains" and operand in value:
            return True
    return False


SUBSCRIBERS = {
    "owner": (COGNITO["owner"][0], True),
    "listed": (COGNITO["listed"][0], True),
    "other": (COGNITO["other"][0], False),
    "suffix-of-entry": (COGNITO["suffix-of-entry"][0], False),
    "prefix-of-entry": (COGNITO["prefix-of-entry"][0], False),
    "spans-two-entries": (COGNITO["spans-two-entries"][0], False),
    "holds-a-separator": ({"username": f"{OTHER_LISTED}, {LISTED}", "groups": []}, False),
}


@pytest.mark.parametrize("caller", list(SUBSCRIBERS))
@pytest.mark.parametrize(
    "published",
    [f"[{LISTED}]", f"[{LISTED}, {OTHER_LISTED}]", f"[{OTHER_LISTED}, {LISTED}]"],
)
def test_a_subscriber_receives_exactly_the_messages_meant_for_them(
    client, caller: str, published: str
) -> None:
    identity, expected = SUBSCRIBERS[caller]
    rules = _subscription_filter(client, identity)
    assert _filter_matches(rules, {"Owner": OWNER, "SharedWith": published}) is expected


def test_a_name_holding_a_separator_is_matched_as_the_owner_only(client) -> None:
    rules = _subscription_filter(client, {"username": f"[{LISTED}", "groups": []})
    assert rules == {"or": [{"Owner": {"eq": f"[{LISTED}"}}]}


def test_an_admin_subscription_is_not_filtered(client) -> None:
    assert _subscription_filter(client, COGNITO["admin"][0]) is None


# ── meetingAccess.js: chat and VNC preview subscriptions, VNC preview toggle ──


def _evaluate_chat(client, function: str, context: dict) -> dict:
    return client.evaluate_code(
        runtime={"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"},
        code=(APPSYNC / "meetingAccess.js").read_text(encoding="utf-8"),
        context=json.dumps(context),
        function=function,
    )


# The arguments of each operation meetingAccess.js answers: the subscriptions
# name the meeting as CallId, the toggleVNCPreview mutation as input.CallId.
MEETING_ACCESS_ARGUMENTS = {
    "onSendChatMessage": {"CallId": "c1", "MessageId": "m1"},
    "onVNCPreviewToggle": {"CallId": "c1"},
    "toggleVNCPreview": {"input": {"CallId": "c1", "Show": True}},
}


@pytest.mark.parametrize("operation", list(MEETING_ACCESS_ARGUMENTS))
@pytest.mark.parametrize("shape,caller,identity,expected", CASES)
def test_a_meeting_access_check_answers_exactly_the_people_the_meeting_is_for(
    client, shape: str, caller: str, identity: dict, expected: bool, operation: str
) -> None:
    arguments = MEETING_ACCESS_ARGUMENTS[operation]
    request = _evaluate_chat(client, "request", {"arguments": arguments, "identity": identity})
    assert not request.get("error"), request
    if caller == "admin":
        # Answered without reading the meeting.
        assert json.loads(request["evaluationResult"]) is None
        return
    assert json.loads(request["evaluationResult"]) == {
        "operation": "GetItem",
        "key": {"PK": {"S": "c#c1"}, "SK": {"S": "c#c1"}},
        "consistentRead": True,
    }
    meeting = {"PK": "c#c1", "SK": "c#c1", "Owner": OWNER, "SharedWith": SHAPES[shape]}
    response = _evaluate_chat(
        client, "response", {"arguments": arguments, "identity": identity, "result": meeting}
    )
    assert (not response.get("error")) is expected, response


@pytest.mark.parametrize("operation", list(MEETING_ACCESS_ARGUMENTS))
def test_a_meeting_access_check_answers_a_backend_role(client, operation: str) -> None:
    request = _evaluate_chat(
        client,
        "request",
        {"arguments": MEETING_ACCESS_ARGUMENTS[operation], "identity": BACKEND},
    )
    assert not request.get("error") and json.loads(request["evaluationResult"]) is None


def test_a_vnc_preview_toggle_is_recorded_for_the_meeting(client) -> None:
    response = client.evaluate_code(
        runtime={"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"},
        code=(APPSYNC / "toggleVNCPreview.js").read_text(encoding="utf-8"),
        context=json.dumps(
            {
                "arguments": MEETING_ACCESS_ARGUMENTS["toggleVNCPreview"],
                "identity": BACKEND,
                "prev": {"result": None},
            }
        ),
        function="request",
    )
    assert not response.get("error"), response
    request = json.loads(response["evaluationResult"])
    assert request["operation"] == "PutItem"
    assert request["key"]["PK"] == {"S": "vnc#c1"}
    assert request["attributeValues"]["Action"] == {"S": "open"}


def test_the_vnc_preview_pipeline_answers_with_the_recorded_event(client) -> None:
    import yaml  # imported here so the module collects without PyYAML when skipped
    from test_shared_with_access import TEMPLATE, _CfnLoader, _resolver_for

    resources = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]
    code = _resolver_for(resources, "Mutation", "toggleVNCPreview")["Code"]
    event = {"CallId": "c1", "Action": "open", "Success": True}
    response = client.evaluate_code(
        runtime={"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"},
        code=code,
        context=json.dumps({"arguments": {}, "prev": {"result": event}}),
        function="response",
    )
    assert not response.get("error"), response
    assert json.loads(response["evaluationResult"]) == event


def test_a_chat_subscription_to_a_missing_meeting_is_refused(client) -> None:
    response = _evaluate_chat(
        client,
        "response",
        {"arguments": {"CallId": "c1"}, "identity": COGNITO["owner"][0], "result": None},
    )
    assert response.get("error")


def test_a_signed_in_chat_subscriber_names_the_meeting(client) -> None:
    request = _evaluate_chat(client, "request", {"arguments": {}, "identity": COGNITO["owner"][0]})
    assert request.get("error")


# ── Virtual Participant writes ──────────────────────────────────────────────


def _inline(field: str, key: str) -> str:
    """The write's request or response template; for a pipeline, its last
    function's."""
    import yaml  # imported here so the module collects without PyYAML when skipped
    from test_shared_with_access import (
        TEMPLATE,
        _CfnLoader,
        _request_template,
        _response_template,
    )

    resources = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]
    if key == "RequestMappingTemplate":
        template = _request_template(resources, field)
    else:
        template = _response_template(resources, field)
    if isinstance(template, dict):
        # shareCall's template is an Fn::Sub: substitute it as CloudFormation would.
        template = template["Fn::Sub"].replace("${EventSourcingTable}", "events")
        template = template.replace("${!", "${")
    return template


def _evaluate_inline(client, field: str, key: str, context: dict) -> dict:
    return client.evaluate_mapping_template(
        template=_inline(field, key), context=json.dumps(context)
    )


VP_INPUTS = {
    "shareVirtualParticipant": {"id": "vp1", "SharedWith": [LISTED, OTHER_LISTED]},
    "unshareVirtualParticipant": {"id": "vp1", "SharedWith": [LISTED]},
    "updateVirtualParticipant": {"id": "vp1", "status": "JOINING"},
    "acknowledgeVPFailure": None,
}


def _vp_arguments(field: str) -> dict:
    if field == "acknowledgeVPFailure":
        return {"virtualParticipantId": "vp1"}
    return {"input": VP_INPUTS[field]}


# Whether the caller is held to the row's Owner.
VP_CALLERS = {
    "owner": (COGNITO["owner"][0], True),
    "listed": (COGNITO["listed"][0], True),
    "other": (COGNITO["other"][0], True),
    "admin": (COGNITO["admin"][0], False),
    "backend": (BACKEND, False),
}


@pytest.mark.parametrize("caller", list(VP_CALLERS))
@pytest.mark.parametrize("field", list(VP_INPUTS))
def test_a_virtual_participant_write_is_conditioned_on_its_owner(
    client, field: str, caller: str
) -> None:
    """A signed-in caller who is not an Admin writes only a row whose Owner is
    them: for a user the row is shared with, as for anyone else, the condition
    fails. Admins and backend roles are not held to it."""
    identity, restricted = VP_CALLERS[caller]
    response = _evaluate_inline(
        client,
        field,
        "RequestMappingTemplate",
        {"arguments": _vp_arguments(field), "identity": identity},
    )
    assert not response.get("error"), response
    condition = json.loads(response["evaluationResult"]).get("condition")
    if field != "updateVirtualParticipant":
        assert condition["expression"].startswith("attribute_exists(#conditionId)")
        assert condition["expressionNames"]["#conditionId"] == "id"
    if restricted:
        assert "#conditionOwner = :conditionCaller" in condition["expression"]
        assert condition["expressionNames"]["#conditionOwner"] == "Owner"
        assert condition["expressionValues"][":conditionCaller"] == {"S": identity["username"]}
    else:
        assert condition is None or "#conditionOwner" not in condition["expression"]
    assert json.loads(response["stash"])["vpCallerRestricted"] is restricted


def test_the_owner_condition_is_anded_with_the_status_condition(client) -> None:
    response = _evaluate_inline(
        client,
        "updateVirtualParticipant",
        "RequestMappingTemplate",
        {
            "arguments": {"input": VP_INPUTS["updateVirtualParticipant"]},
            "identity": COGNITO["owner"][0],
        },
    )
    condition = json.loads(response["evaluationResult"])["condition"]
    assert condition["expression"] == (
        "attribute_not_exists(#conditionEndReason) AND #conditionOwner = :conditionCaller"
    )


def test_a_virtual_participant_is_shared_with_its_entries_as_stored(client) -> None:
    recipients = [f" {LISTED} ", "", LISTED, OTHER_LISTED]
    response = _evaluate_inline(
        client,
        "shareVirtualParticipant",
        "RequestMappingTemplate",
        {
            "arguments": {"input": {"id": "vp1", "SharedWith": recipients}},
            "identity": COGNITO["owner"][0],
        },
    )
    update = json.loads(response["evaluationResult"])["update"]
    assert update["expressionValues"][":val"] == {"S": f"{LISTED},{OTHER_LISTED}"}


@pytest.mark.parametrize("recipient", [f"[{LISTED}", f"{LISTED}]", f"{OTHER_LISTED}, {LISTED}"])
@pytest.mark.parametrize("field", ["shareVirtualParticipant", "shareCall"])
def test_a_recipient_holding_a_separator_is_refused(client, field: str, recipient: str) -> None:
    inputs = {
        "shareVirtualParticipant": {"id": "vp1", "SharedWith": [OTHER_LISTED, recipient]},
        "shareCall": {
            "CallId": "c1",
            "ListPK": "l",
            "ListSK": "s",
            "SharedWith": [OTHER_LISTED, recipient],
        },
    }
    response = _evaluate_inline(
        client,
        field,
        "RequestMappingTemplate",
        {"arguments": {"input": inputs[field]}, "identity": BACKEND},
    )
    assert response.get("error"), response
    assert "cannot contain" in response["error"]["message"]


@pytest.mark.parametrize("shared_with", [[LISTED], None])
def test_a_meeting_is_shared_with_plain_recipients(client, shared_with) -> None:
    response = _evaluate_inline(
        client,
        "shareCall",
        "RequestMappingTemplate",
        {
            "arguments": {
                "input": {"CallId": "c1", "ListPK": "l", "ListSK": "s", "SharedWith": shared_with}
            },
            "identity": BACKEND,
        },
    )
    assert not response.get("error"), response


@pytest.mark.parametrize(
    "field", ["shareVirtualParticipant", "unshareVirtualParticipant", "acknowledgeVPFailure"]
)
@pytest.mark.parametrize("restricted,expected", [(True, "not authorized"), (False, "not found")])
def test_a_refused_virtual_participant_share_says_why(
    client, field: str, restricted: bool, expected: str
) -> None:
    response = _evaluate_inline(
        client,
        field,
        "ResponseMappingTemplate",
        {
            "arguments": _vp_arguments(field),
            "identity": COGNITO["other"][0],
            "stash": {"vpCallerRestricted": restricted},
            "error": {
                "message": "The conditional request failed",
                "type": "DynamoDB:ConditionalCheckFailedException",
            },
        },
    )
    assert expected in response["error"]["message"]


def test_a_shared_virtual_participant_answers_with_the_row_as_written(client) -> None:
    response = _evaluate_inline(
        client,
        "shareVirtualParticipant",
        "ResponseMappingTemplate",
        {
            "arguments": {"input": {"id": "vp1", "SharedWith": [LISTED], "Owner": "someone"}},
            "identity": COGNITO["owner"][0],
            "result": {"id": "vp1", "Owner": OWNER, "SharedWith": LISTED},
        },
    )
    assert json.loads(response["evaluationResult"]) == {
        "id": "vp1",
        "Owner": OWNER,
        "SharedWith": LISTED,
    }


def test_an_acknowledged_failure_answers_true(client) -> None:
    response = _evaluate_inline(
        client,
        "acknowledgeVPFailure",
        "ResponseMappingTemplate",
        {
            "arguments": _vp_arguments("acknowledgeVPFailure"),
            "identity": COGNITO["owner"][0],
            "result": {"id": "vp1", "userAcknowledgedFailure": True},
        },
    )
    assert not response.get("error"), response
    assert json.loads(response["evaluationResult"]) is True


# ── unsharing a Virtual Participant ─────────────────────────────────────────


def _unshare_read(client, row) -> dict:
    """The stash the read function leaves for the write."""
    import yaml  # imported here so the module collects without PyYAML when skipped
    from test_shared_with_access import TEMPLATE, _CfnLoader

    resources = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]
    function = resources["ReadVirtualParticipantSharingAppSyncFunction"]["Properties"]
    request = client.evaluate_mapping_template(
        template=function["RequestMappingTemplate"],
        context=json.dumps({"arguments": {"input": {"id": "vp1", "SharedWith": [LISTED]}}}),
    )
    assert json.loads(request["evaluationResult"])["key"] == {"id": {"S": "vp1"}}
    response = client.evaluate_mapping_template(
        template=function["ResponseMappingTemplate"], context=json.dumps({"result": row})
    )
    assert not response.get("error"), response
    return json.loads(response["stash"])


def test_the_read_before_unsharing_records_the_row(client) -> None:
    stash = _unshare_read(client, {"id": "vp1", "Owner": OWNER, "SharedWith": "a,b"})
    assert stash == {"vpFound": True, "vpOwnerAsRead": OWNER, "sharedWithAsRead": "a,b"}
    assert _unshare_read(client, {"id": "vp1", "Owner": OWNER}) == {
        "vpFound": True,
        "vpOwnerAsRead": OWNER,
    }
    assert _unshare_read(client, None) == {"vpFound": False}


def _unshare_write(client, removed, stash: dict, identity=None) -> dict:
    response = _evaluate_inline(
        client,
        "unshareVirtualParticipant",
        "RequestMappingTemplate",
        {
            "arguments": {"input": {"id": "vp1", "SharedWith": removed}},
            "identity": identity or COGNITO["owner"][0],
            "stash": stash,
        },
    )
    assert not response.get("error"), response
    return json.loads(response["evaluationResult"])


CAROL = "carol@example.com"


@pytest.mark.parametrize(
    "as_read,removed,kept",
    [
        (f"{OTHER_LISTED},{LISTED},{CAROL}", [LISTED], f"{OTHER_LISTED},{CAROL}"),
        (f"[{OTHER_LISTED}, {LISTED}]", [f" {LISTED} "], OTHER_LISTED),
        ([OTHER_LISTED, LISTED], [LISTED, CAROL], OTHER_LISTED),
        (f"{OTHER_LISTED},{LISTED}", [OTHER_LISTED, LISTED], ""),
        (f"{OTHER_LISTED},{OTHER_LISTED},{LISTED}", [CAROL], f"{OTHER_LISTED},{LISTED}"),
        (f"{OTHER_LISTED},{LISTED}", [], f"{OTHER_LISTED},{LISTED}"),
        # Part of an entry is not the entry.
        (f"{OTHER_LISTED},{LISTED}", ["ob@example.com"], f"{OTHER_LISTED},{LISTED}"),
    ],
)
def test_unsharing_removes_only_the_entries_named(client, as_read, removed, kept: str) -> None:
    request = _unshare_write(client, removed, {"vpFound": True, "sharedWithAsRead": as_read})
    assert request["update"]["expressionValues"][":val"] == {"S": kept}
    condition = request["condition"]
    assert "#conditionSharedWith = :conditionSharedWithAsRead" in condition["expression"]
    assert condition["expressionNames"]["#conditionSharedWith"] == "SharedWith"
    values = condition["expressionValues"]
    if isinstance(as_read, list):
        # A List and a string set are read alike, so either is accepted.
        assert values[":conditionSharedWithAsRead"] == {"L": [{"S": v} for v in as_read]}
        assert values[":conditionSharedWithAsReadSet"] == {"SS": as_read}
        assert (
            "(#conditionSharedWith = :conditionSharedWithAsRead OR "
            "#conditionSharedWith = :conditionSharedWithAsReadSet)"
        ) in condition["expression"]
    else:
        assert values[":conditionSharedWithAsRead"] == {"S": as_read}
        assert ":conditionSharedWithAsReadSet" not in values


@pytest.mark.parametrize("identity", [COGNITO["owner"][0], COGNITO["admin"][0], BACKEND])
def test_unsharing_a_row_without_shared_with_requires_it_still_absent(client, identity) -> None:
    request = _unshare_write(client, [LISTED], {"vpFound": True}, identity)
    assert request["update"]["expressionValues"][":val"] == {"S": ""}
    condition = request["condition"]
    assert condition["expression"].startswith("attribute_exists(#conditionId)")
    assert "attribute_not_exists(#conditionSharedWith)" in condition["expression"]
    if identity is COGNITO["owner"][0]:
        assert "#conditionOwner = :conditionCaller" in condition["expression"]
    else:
        assert "#conditionOwner" not in condition["expression"]
        assert "expressionValues" not in condition


def _unshare_refusal(client, stash: dict, identity: dict) -> str:
    response = _evaluate_inline(
        client,
        "unshareVirtualParticipant",
        "ResponseMappingTemplate",
        {
            "arguments": {"input": {"id": "vp1", "SharedWith": [LISTED]}},
            "identity": identity,
            "stash": stash,
            "error": {
                "message": "The conditional request failed",
                "type": "DynamoDB:ConditionalCheckFailedException",
            },
        },
    )
    return response["error"]["message"]


def test_a_refused_unshare_says_why(client) -> None:
    found = {"vpFound": True, "vpOwnerAsRead": OWNER}
    listed, owner = COGNITO["listed"][0], COGNITO["owner"][0]
    assert "not authorized" in _unshare_refusal(
        client, {**found, "vpCallerRestricted": True}, listed
    )
    assert "changed while it was being updated" in _unshare_refusal(
        client, {**found, "vpCallerRestricted": True}, owner
    )
    assert "changed while it was being updated" in _unshare_refusal(
        client, {**found, "vpCallerRestricted": False}, COGNITO["admin"][0]
    )
    assert "not found" in _unshare_refusal(
        client, {"vpFound": False, "vpCallerRestricted": False}, BACKEND
    )


def test_an_unshare_answers_with_the_entries_removed(client) -> None:
    response = _evaluate_inline(
        client,
        "unshareVirtualParticipant",
        "ResponseMappingTemplate",
        {
            "arguments": {"input": {"id": "vp1", "SharedWith": [LISTED]}},
            "identity": COGNITO["owner"][0],
            "result": {"id": "vp1", "SharedWith": OTHER_LISTED},
        },
    )
    assert json.loads(response["evaluationResult"]) == {"id": "vp1", "SharedWith": [LISTED]}
