# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting chat message through the Kinesis pipeline, end to end.

A Virtual Participant adds the meeting chat to the transcript as
ADD_TRANSCRIPT_SEGMENT events on the MEETING_CHAT channel (see
lma-virtual-participant-stack/backend/src/meeting-chat.ts). This probe puts the
same events on the CallDataStream that a VP would -- a START, then one chat
segment -- and follows the segment through the stack:

1. the call event processor stores it under the meeting's transcript partition,
   with an expiry like a speech segment's;
2. AppSync's getTranscriptSegments returns it, so the meeting page shows it;
3. the FetchTranscript function, which the summary, the knowledge base export
   and the meeting assistant read the transcript through, leaves it out;
4. the transcript deletion function removes it with the rest of the transcript.

Self-contained: it needs no meeting, no Cognito user and no setting change. The
deletion is invoked with the ``scheduled`` trigger, which deletes the one
synthetic meeting's transcript at once whatever the Retention section says.

It also follows a chat attachment the same way (``send_attachment``): a
Markdown file put in the meeting's attachments folder, as the VP stores one
(chat-attachments.ts), and the ADD_CHAT_ATTACHMENT event naming it. The call
event processor stores the attachment's item and queues the summarizer, which
writes its AI title and summary; the transcript deletion removes the item and
the file.

Used by test_meeting_chat_segment_is_stored_read_and_deleted and
test_chat_attachment_is_summarized_and_deleted_with_the_transcript in
test_lma_integration.py, and can be run standalone:

    AWS_PROFILE=default python integ-tests/meeting_chat_probe.py lma-integtest1
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer

from kds_pipeline_probe import _stack_resources, _stream_name

CHAT_CHANNEL = "MEETING_CHAT"
ATTACHMENTS_PREFIX = "lma-chat-attachments/"
#: A short Markdown file, the type whose summary the live test of #1185 found
#: missing on a reasoning model.
ATTACHMENT_NAME = "integ-notes.md"
ATTACHMENT_BODY = (
    b"# Q3 launch checklist\n\n"
    b"- Freeze the release branch on 3 March\n"
    b"- Finish the pricing page\n"
    b"- Brief the support team\n"
)
OWNER = "integ-test@lma.aws"
SENDER = "Integ Chat Sender"


@dataclass
class ChatProbe:
    """The stack's resources the probe uses, and the synthetic meeting it made."""

    session: Any
    stream: str
    table: str
    fetch_transcript_function: str
    deletion_function: Optional[str]
    call_id: str
    segment_id: str
    text: str
    bucket: Optional[str] = None
    attachment_id: Optional[str] = None

    @classmethod
    def for_stack(cls, stack_name: str, region: Optional[str] = None) -> "ChatProbe":
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
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        parameters = cfn.describe_stacks(StackName=ai_stack)["Stacks"][0].get("Parameters", [])
        bucket = next((p["ParameterValue"] for p in parameters if p["ParameterKey"] == "S3BucketName"), None)
        return cls(
            session=session,
            stream=_stream_name(cfn, stack_name),
            table=ai["EventSourcingTable"],
            fetch_transcript_function=ai["FetchTranscript"],
            deletion_function=ai.get("TranscriptDeletionFunction"),
            call_id=f"integ-test-chat-{stamp}",
            segment_id=f"chat-integ-{stamp}",
            # A marker no transcript would hold by chance, plus a link, as a
            # participant's message would carry one.
            text=f"integ chat marker {stamp} https://example.com/integ-{stamp}",
            bucket=bucket,
            # Zero-padded milliseconds plus a suffix, as attachmentIdFor writes.
            attachment_id=f"0000001500-{stamp[-8:]}",
        )

    # ── the events a Virtual Participant sends ────────────────────────────────

    def _put(self, record: Dict[str, Any]) -> None:
        self.session.client("kinesis").put_record(
            StreamName=self.stream,
            Data=json.dumps(record).encode("utf-8"),
            PartitionKey=self.call_id,
        )

    def reads_every_meeting(self) -> bool:
        """Whether this probe's AppSync reads see every meeting's transcript.

        getTranscriptSegments returns a non-Admin caller only segments whose
        Owner or SharedWith names it, and the CallEventProcessor writes a
        segment's SharedWith but not its Owner. AppSync treats an assumed role
        (the CI runner, a Lambda function) as the backend and returns every
        segment; an IAM user (a developer's machine) is treated as a user, so
        it cannot read back a synthetic meeting's segments.
        """
        arn = self.session.client("sts").get_caller_identity().get("Arn", "")
        return ":assumed-role/" in arn

    def send_start(self) -> None:
        self._put(
            {
                "EventType": "START",
                "CallId": self.call_id,
                "CustomerPhoneNumber": "Virtual Participant",
                "SystemPhoneNumber": "LMA Integ Test",
                "AgentId": OWNER,
                "CreatedAt": datetime.now(timezone.utc).isoformat(),
                "AccessToken": "",
                "IdToken": "",
                "RefreshToken": "",
            }
        )

    def send_chat_segment(self) -> None:
        """The record kinesis-stream.ts sendChatSegment writes, field for field."""
        now = datetime.now(timezone.utc).isoformat()
        self._put(
            {
                "EventType": "ADD_TRANSCRIPT_SEGMENT",
                "CallId": self.call_id,
                "Channel": CHAT_CHANNEL,
                "SegmentId": self.segment_id,
                "StartTime": 1.5,
                "EndTime": 1.5,
                "Transcript": self.text,
                "IsPartial": False,
                "CreatedAt": now,
                "UpdatedAt": now,
                "Sentiment": None,
                "TranscriptEvent": None,
                "Speaker": SENDER,
                "AccessToken": "",
                "IdToken": "",
                "RefreshToken": "",
            }
        )

    def attachment_folder(self) -> str:
        """The meeting's attachments folder, named as chat-attachments.ts names it."""
        name = re.sub(r"_+$", "", re.sub(r"^_+", "", re.sub(r"[^a-zA-Z0-9_.]", "_", self.call_id)))
        return f"{ATTACHMENTS_PREFIX}{name}/"

    def attachment_key(self) -> str:
        return f"{self.attachment_folder()}{self.attachment_id}/{ATTACHMENT_NAME}"

    def send_attachment(self) -> None:
        """Store a Markdown file as the VP does, and send the event it sends, field for field."""
        if not self.bucket:
            raise RuntimeError("the AI stack names no S3BucketName")
        self.session.client("s3").put_object(
            Bucket=self.bucket,
            Key=self.attachment_key(),
            Body=ATTACHMENT_BODY,
            ContentType="text/markdown",
            ContentDisposition="attachment",
        )
        self._put(
            {
                "EventType": "ADD_CHAT_ATTACHMENT",
                "CallId": self.call_id,
                "AttachmentId": self.attachment_id,
                "SegmentId": self.segment_id,
                "Timestamp": 1.5,
                "FileName": ATTACHMENT_NAME,
                "Platform": "zoom",
                "Kind": "DOCUMENT",
                "Status": "STORED",
                "ContentType": "text/markdown",
                "Size": len(ATTACHMENT_BODY),
                "S3Key": self.attachment_key(),
                "Summarize": True,
                "CreatedAt": datetime.now(timezone.utc).isoformat(),
                "AccessToken": "",
                "IdToken": "",
                "RefreshToken": "",
            }
        )

    def attachment_item(self) -> Optional[Dict[str, Any]]:
        items = self._items(f"att#{self.call_id}")
        return next((i for i in items if i.get("SK") == f"a#{self.attachment_id}"), None)

    def summarized_attachment(self) -> Optional[Dict[str, Any]]:
        item = self.attachment_item()
        return item if item and item.get("SummaryState") else None

    def attachment_files(self) -> List[str]:
        """Every version of every file left in the meeting's attachments folder."""
        if not self.bucket:
            return []
        found: List[str] = []
        paginator = self.session.client("s3").get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.attachment_folder()):
            found += [v["Key"] for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        return found

    # ── reading back ──────────────────────────────────────────────────────────

    def _items(self, pk: str) -> List[Dict[str, Any]]:
        ddb = self.session.client("dynamodb")
        d = TypeDeserializer()
        kwargs: Dict[str, Any] = {
            "TableName": self.table,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
            "ConsistentRead": True,
        }
        items: List[Dict[str, Any]] = []
        while True:
            resp = ddb.query(**kwargs)
            items += [{k: d.deserialize(v) for k, v in i.items()} for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def meeting(self) -> Optional[Dict[str, Any]]:
        items = self._items(f"c#{self.call_id}")
        return next((i for i in items if i.get("SK") == f"c#{self.call_id}"), None)

    def transcript_items(self) -> List[Dict[str, Any]]:
        return self._items(f"trs#{self.call_id}")

    def stored_segment(self) -> Optional[Dict[str, Any]]:
        return next((i for i in self.transcript_items() if i.get("SK") == f"s#{self.segment_id}"), None)

    def appsync_segments(self, graphql: Callable[..., Dict[str, Any]]) -> List[Dict[str, Any]]:
        data = graphql(
            "query Q($callId: ID!) { getTranscriptSegments(callId: $callId) "
            "{ TranscriptSegments { SegmentId Channel Speaker Transcript StartTime EndTime IsPartial } } }",
            {"callId": self.call_id},
        )
        return list((data.get("getTranscriptSegments") or {}).get("TranscriptSegments") or [])

    def _invoke(self, function: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.client("lambda").invoke(
            FunctionName=function,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        body = json.loads(response["Payload"].read() or b"null")
        if response.get("FunctionError"):
            raise RuntimeError(f"{function} failed: {body}")
        return body

    def fetch_transcript(self) -> str:
        body = self._invoke(
            self.fetch_transcript_function,
            {"CallId": self.call_id, "IncludeSpeaker": True, "ProcessTranscript": False},
        )
        return str((body or {}).get("transcript", ""))

    def delete_transcript(self) -> Dict[str, Any]:
        if not self.deletion_function:
            raise RuntimeError("the stack has no TranscriptDeletionFunction")
        return self._invoke(self.deletion_function, {"CallId": self.call_id, "Trigger": "scheduled"})

    def cleanup(self) -> None:
        """Best effort: the synthetic meeting's record and anything left of its transcript."""
        ddb = self.session.client("dynamodb")
        if self.bucket:
            try:
                s3 = self.session.client("s3")
                paginator = s3.get_paginator("list_object_versions")
                for page in paginator.paginate(Bucket=self.bucket, Prefix=self.attachment_folder()):
                    for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
                        s3.delete_object(Bucket=self.bucket, Key=v["Key"], VersionId=v["VersionId"])
            except Exception:  # noqa: BLE001
                pass
        for pk in (f"c#{self.call_id}", f"trs#{self.call_id}", f"att#{self.call_id}"):
            try:
                for item in self._items(pk):
                    ddb.delete_item(
                        TableName=self.table,
                        Key={"PK": {"S": item["PK"]}, "SK": {"S": item["SK"]}},
                    )
            except Exception:  # noqa: BLE001
                pass


def wait_for(predicate: Callable[[], Any], timeout: float, interval: float = 3.0) -> Any:
    """Poll ``predicate`` until it returns something truthy, or the time runs out."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


if __name__ == "__main__":
    probe = ChatProbe.for_stack(sys.argv[1] if len(sys.argv) > 1 else "LMA")
    try:
        probe.send_start()
        print("meeting created" if wait_for(probe.meeting, 90) else "NO MEETING")
        probe.send_chat_segment()
        print(json.dumps(wait_for(probe.stored_segment, 60), default=str, indent=2))
        print("in FetchTranscript:", probe.text in probe.fetch_transcript())
        print("deletion:", probe.delete_transcript())
        print("left after deletion:", len(probe.transcript_items()))
    finally:
        probe.cleanup()
