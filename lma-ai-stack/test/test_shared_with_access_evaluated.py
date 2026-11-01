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
