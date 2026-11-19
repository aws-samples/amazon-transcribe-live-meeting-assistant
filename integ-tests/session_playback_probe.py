# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A continued meeting's per-session playback fields, through the Kinesis pipeline.

The meeting page's player plays each capture session's recording at the
session's place on the transcript timeline (docs/virtual-participant.md#recordings-and-playback).
It reads three things from the meeting record, which this probe follows through
the deployed call event processor and AppSync resolvers:

1. a recording link stored with its session (``RecordingUrlSessions`` beside
   ``RecordingUrls``), for an event that names the session, as a Virtual
   Participant's does, and for one that does not, as a streaming client's
   transcriber's does, which takes the meeting's current session;
2. a later session listed with its offset (``Sessions``) when its START reopens
   the meeting.

Session 2 is allowed on the meeting record the way a rejoin or a reopen allows
it, by writing ``SessionNumber`` and ``SessionOffsetSeconds``, so the probe
needs no Virtual Participant, no Cognito user and no setting change. The links
name no object; nothing is uploaded. Used by
test_session_playback.py, and can be run standalone:

    AWS_PROFILE=default python integ-tests/session_playback_probe.py lma-integtest1
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer

from kds_pipeline_probe import _stack_resources, _stream_name

OWNER = "integ-test@lma.aws"
#: Where session 2 starts on the transcript timeline, as a rejoin would record it.
SESSION_2_OFFSET = 317


@dataclass
class SessionPlaybackProbe:
    session: Any
    stream: str
    table: str
    bucket: str
    call_id: str

    @classmethod
    def for_stack(cls, stack_name: str, region: Optional[str] = None) -> "SessionPlaybackProbe":
        session = boto3.Session(region_name=region)
        cfn = session.client("cloudformation")
        ai_stack = next(
            (
                r["PhysicalResourceId"]
                for r in _stack_resources(cfn, stack_name)
                if r["LogicalResourceId"] == "AISTACK"
            ),
            None,
        )
        if not ai_stack:
            raise RuntimeError(f"{stack_name} has no AISTACK nested stack")
        ai = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, ai_stack)}
        parameters = cfn.describe_stacks(StackName=ai_stack)["Stacks"][0].get("Parameters", [])
        bucket = next(
            (p["ParameterValue"] for p in parameters if p["ParameterKey"] == "S3BucketName"),
            "bucket",
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        return cls(
            session=session,
            stream=_stream_name(cfn, stack_name),
            table=ai["EventSourcingTable"],
            bucket=bucket,
            call_id=f"integ-test-sessions-{stamp}",
        )

    def link(self, session_number: int) -> str:
        return (
            f"https://{self.bucket}.s3.amazonaws.com/lma-audio-recordings/"
            f"{self.call_id}_s{session_number}.wav"
        )

    def _put(self, record: Dict[str, Any]) -> None:
        self.session.client("kinesis").put_record(
            StreamName=self.stream,
            Data=json.dumps(
                {**record, "CallId": self.call_id, "AccessToken": "", "IdToken": "", "RefreshToken": ""}
            ).encode("utf-8"),
            PartitionKey=self.call_id,
        )

    def send_start(self, session_number: Optional[int] = None) -> None:
        record: Dict[str, Any] = {
            "EventType": "START",
            "CustomerPhoneNumber": "Integ Test Participant",
            "SystemPhoneNumber": "LMA Integ Test",
            "AgentId": OWNER,
            "CreatedAt": datetime.now(timezone.utc).isoformat(),
        }
        if session_number is not None:
            record["SessionNumber"] = session_number
        self._put(record)

    def send_recording(self, session_number: int, *, name_the_session: bool) -> None:
        record: Dict[str, Any] = {
            "EventType": "ADD_S3_RECORDING_URL",
            "RecordingUrl": self.link(session_number),
        }
        if name_the_session:
            record["SessionNumber"] = session_number
        self._put(record)

    def allow_session(self, session_number: int, offset: int) -> None:
        """Record on the meeting the session its next START may open, as
        lma_sessions.allow_session does for a rejoin or a reopen."""
        self.session.resource("dynamodb").Table(self.table).update_item(
            Key={"PK": f"c#{self.call_id}", "SK": f"c#{self.call_id}"},
            UpdateExpression=(
                "SET SessionNumber = :session, SessionOffsetSeconds = :offset, "
                "ReopenedAt = :now, ReopenedBy = :by"
            ),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={
                ":session": session_number,
                ":offset": Decimal(offset),
                ":now": datetime.now(timezone.utc).isoformat(),
                ":by": OWNER,
            },
        )

    def meeting(self) -> Optional[Dict[str, Any]]:
        key = {"PK": {"S": f"c#{self.call_id}"}, "SK": {"S": f"c#{self.call_id}"}}
        item = (
            self.session.client("dynamodb")
            .get_item(TableName=self.table, Key=key, ConsistentRead=True)
            .get("Item")
        )
        if not item:
            return None
        deserializer = TypeDeserializer()
        return {name: deserializer.deserialize(value) for name, value in item.items()}

    def cleanup(self) -> None:
        """Delete the synthetic meeting's items (every item under PK = c#<callId>)."""
        ddb = self.session.client("dynamodb")
        pk = {"S": f"c#{self.call_id}"}
        for page in ddb.get_paginator("query").paginate(
            TableName=self.table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": pk},
            ProjectionExpression="PK, SK",
        ):
            for item in page.get("Items", []):
                ddb.delete_item(TableName=self.table, Key={"PK": item["PK"], "SK": item["SK"]})


def wait_for(read: Callable[[], Any], ready: Callable[[Any], bool], timeout_s: float) -> Any:
    """Read until `ready` holds or the time is up; returns the last read."""
    deadline = time.monotonic() + timeout_s
    value = read()
    while not ready(value) and time.monotonic() < deadline:
        time.sleep(3)
        value = read()
    return value


def main(stack_name: str) -> int:
    probe = SessionPlaybackProbe.for_stack(stack_name)
    print(f"meeting {probe.call_id}")
    try:
        probe.send_start()
        print("created:", bool(wait_for(probe.meeting, bool, 90)))
        probe.send_recording(1, name_the_session=True)
        probe.allow_session(2, SESSION_2_OFFSET)
        probe.send_start(2)
        probe.send_recording(2, name_the_session=False)
        meeting = wait_for(
            probe.meeting, lambda m: len((m or {}).get("RecordingUrlSessions") or []) >= 2, 90
        )
        for field in ("RecordingUrls", "RecordingUrlSessions", "Sessions", "SessionStartedNumber"):
            print(f"{field}: {(meeting or {}).get(field)}")
    finally:
        probe.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "LMA"))
