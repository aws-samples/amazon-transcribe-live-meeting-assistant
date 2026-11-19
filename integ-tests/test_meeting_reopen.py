# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Reopening an ended meeting with the SDK (``client.meetings.reopen``), on a live stack.

The test makes its own synthetic meeting: a START on the CallDataStream, as
the Virtual Participant and the transcriber send one, which the call event
processor turns into a meeting record. It refuses to reopen it while it is
live, then marks it ended on the record (an END would run the end-of-meeting
work, which a synthetic meeting has nothing for), and reopens it for session
2, which is then held: a second reopen is refused while that session has not
started. Nothing streams, so no session starts and the meeting stays ended.
Self-cleaning.

``reopenMeeting`` accepts an IAM caller that signs with an assumed role (an
SSO or role profile, or the CI runner's role); with an IAM user's keys the
test is skipped, saying so.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import boto3
import pytest

from kds_pipeline_probe import _event_sourcing_table, _stream_name
from lma_sdk import LMAClient
from lma_sdk.exceptions import LMAAppSyncError


def _assumed_role(session: boto3.Session) -> bool:
    return ":assumed-role/" in session.client("sts").get_caller_identity().get("Arn", "")


def test_an_ended_meeting_is_reopened_for_its_next_session(client: LMAClient) -> None:
    session = client.session
    if not _assumed_role(session):
        pytest.skip("reopenMeeting accepts an assumed role over IAM; these credentials are an IAM user's")
    cfn = session.client("cloudformation")
    ddb = session.client("dynamodb")
    stream = _stream_name(cfn, client.stack_name)
    table = _event_sourcing_table(cfn, ddb, client.stack_name)
    call_id = f"integ-test-reopen-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
    key = {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}

    def meeting() -> dict:
        return ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item") or {}

    try:
        session.client("kinesis").put_record(
            StreamName=stream,
            PartitionKey=call_id,
            Data=json.dumps(
                {
                    "EventType": "START",
                    "CallId": call_id,
                    "CustomerPhoneNumber": "Integ Test Participant",
                    "SystemPhoneNumber": "LMA Integ Test",
                    "AgentId": "integ-test@lma.aws",
                    "CreatedAt": datetime.now(timezone.utc).isoformat(),
                    "AccessToken": "",
                    "IdToken": "",
                    "RefreshToken": "",
                }
            ).encode("utf-8"),
        )
        deadline = time.monotonic() + 90
        while not meeting() and time.monotonic() < deadline:
            time.sleep(3)
        assert meeting(), f"no meeting record for {call_id} within 90s"

        # A live meeting is not reopened.
        with pytest.raises(LMAAppSyncError) as live:
            client.meetings.reopen(call_id)
        assert live.value.error_type == "MeetingInProgress", live.value

        ddb.update_item(
            TableName=table,
            Key=key,
            UpdateExpression="SET #s = :ended",
            ExpressionAttributeNames={"#s": "Status"},
            ExpressionAttributeValues={":ended": {"S": "ENDED"}},
        )

        first = client.meetings.reopen(call_id)
        assert first.call_id == call_id
        assert first.session_number == 2
        assert first.session_offset_seconds >= 0
        record = meeting()
        assert record["SessionNumber"] == {"N": "2"}
        assert record["Status"] == {"S": "ENDED"}, "the meeting reopens when its session starts"

        # While that session has not started, and for 15 minutes, the meeting
        # is not reopened again.
        with pytest.raises(LMAAppSyncError) as pending:
            client.meetings.reopen(call_id)
        assert pending.value.error_type == "MeetingInProgress", pending.value
        assert meeting()["SessionNumber"] == {"N": "2"}

        with pytest.raises(LMAAppSyncError) as missing:
            client.meetings.reopen(f"{call_id}-no-such-meeting")
        assert missing.value.error_type == "MeetingNotFound"
    finally:
        ddb.delete_item(TableName=table, Key=key)
