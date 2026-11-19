# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting record's per-session playback fields, as the resolvers write them.

A meeting continued by a rejoin or a reopen is recorded in one file per capture
session. The meeting page's player plays each session's file at the session's
place on the transcript timeline, and reads three things from the record:

* ``RecordingUrls`` / ``VideoRecordingUrls`` -- every link, oldest first
  (updateCall.request.vtl, for updateRecordingUrl and updateVideoRecordingUrl);
* ``RecordingUrlSessions`` / ``VideoRecordingUrlSessions`` -- the session of
  each link at the same index, appended in the same update;
* ``Sessions`` -- each later session's number and offset, appended once by
  startCallSession.request.vtl.

The static tests below run everywhere. The evaluated ones run the templates
through AppSync's EvaluateMappingTemplate API, which touches no resource but
needs AWS credentials, so they run only when LMA_EVALUATE_APPSYNC=1 is set, as
in test_shared_with_access_evaluated.py.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

APPSYNC = Path(__file__).resolve().parents[1] / "source" / "appsync"
UPDATE_CALL = APPSYNC / "updateCall.request.vtl"
START_SESSION = APPSYNC / "startCallSession.request.vtl"


def evaluated(test):
    """Run only with LMA_EVALUATE_APPSYNC=1; allowed to call AWS
    (scripts/testing/no_real_aws.py)."""
    skip = pytest.mark.skipif(
        os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
        reason="evaluates templates with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
    )
    return skip(pytest.mark.real_aws(test))


def test_a_recording_links_session_is_taken_out_before_the_generic_set():
    """It belongs in the session list beside the link, not in an attribute."""
    text = UPDATE_CALL.read_text(encoding="utf-8")
    remove = text.index('$ctx.args.input.remove("${field}Session")')
    generic = text.index("#foreach( $entry in $ctx.args.input.entrySet() )")
    assert remove < generic


def test_the_session_input_is_declared_on_each_link_input():
    """The template reads `${field}Session` for the link field the input
    carries, so each link's input must declare it."""
    text = (APPSYNC / "schema.graphql").read_text(encoding="utf-8")
    for field in ("RecordingUrl", "VideoRecordingUrl"):
        block = text[text.index(f"input Update{field}Input {{") :]
        block = block[: block.index("}")]
        assert f"\t{field}Session: Int\n" in block, field


def test_each_link_list_is_appended_with_its_session_list_in_the_same_update():
    text = UPDATE_CALL.read_text(encoding="utf-8")
    assert '#set( $sessionsName = "${field}Sessions" )' in text
    links = text.index("#${listName} = list_append(")
    sessions = text.index("#${sessionsName} = list_append(")
    # Both inside the one loop over the two link fields.
    loop = text.index('#foreach( $field in ["RecordingUrl", "VideoRecordingUrl"] )')
    assert loop < links < sessions


def test_a_started_session_is_listed_only_with_its_offset():
    text = START_SESSION.read_text(encoding="utf-8")
    guard = text.index("#if( $util.isNumber($ctx.args.input.SessionOffsetSeconds) )")
    append = text.index("#Sessions = list_append(if_not_exists(#Sessions, :emptyList), :session)")
    assert guard < append
    # Listed once per session: the write is conditional on the session not
    # having started.
    assert re.search(r"attribute_not_exists\(#cStarted\) OR #cStarted < :cSession", text)


# ── evaluated by AppSync ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    import boto3  # imported here so the module collects without boto3 when skipped

    return boto3.client("appsync", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _update(client, template: Path, arguments: dict) -> dict:
    response = client.evaluate_mapping_template(
        template=template.read_text(encoding="utf-8"),
        context=json.dumps({"arguments": arguments}),
    )
    assert not response.get("error"), response.get("error")
    # updateCall.request.vtl's key object ends in a comma, which AppSync accepts.
    result = re.sub(r",\s*}", "}", response["evaluationResult"])
    return json.loads(result)["update"]


@pytest.mark.parametrize(
    ("field", "session", "expected"),
    [
        ("RecordingUrl", 2, {"N": 2}),
        ("VideoRecordingUrl", 3, {"N": 3}),
        ("RecordingUrl", None, {"NULL": True}),
    ],
)
@evaluated
def test_evaluated_a_link_is_stored_with_its_session(client, field, session, expected):
    given = {"CallId": "c1", field: "https://bucket/key"}
    if session is not None:
        given[f"{field}Session"] = session
    update = _update(client, UPDATE_CALL, {"input": given})
    values = update["expressionValues"]
    assert values[f":{field}s"] == {"L": [{"S": "https://bucket/key"}]}
    assert values[f":{field}Sessions"] == {"L": [expected]}
    # Not an attribute of its own.
    assert f"#{field}Session" not in update["expressionNames"]


@evaluated
def test_evaluated_a_status_write_touches_no_list(client):
    update = _update(client, UPDATE_CALL, {"input": {"CallId": "c1", "Status": "ENDED"}})
    assert "list_append" not in update["expression"]


@pytest.mark.parametrize("offset", [317.5, None])
@evaluated
def test_evaluated_a_started_session_is_listed_with_its_offset(client, offset):
    given = {"CallId": "c1", "SessionNumber": 2}
    if offset is not None:
        given["SessionOffsetSeconds"] = offset
    update = _update(client, START_SESSION, {"input": given})
    if offset is None:
        assert "#Sessions" not in update["expression"]
        return
    (entry,) = update["expressionValues"][":session"]["L"]
    assert entry["M"]["SessionNumber"] == {"N": 2}
    assert entry["M"]["OffsetSeconds"] == {"N": offset}
    assert "S" in entry["M"]["StartedAt"]
