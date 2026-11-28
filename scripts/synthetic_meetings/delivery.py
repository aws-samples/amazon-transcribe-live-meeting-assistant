# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The AWS half of the synthetic meeting kit: finding the stack, sending, reading, cleaning up.

Every function takes its boto3 clients as arguments, so the tests give it fakes;
boto3 itself is imported only by ``Stack.resolve``. ``pycognito`` and
``websockets`` are imported only by the audio path's functions, so the Kinesis
path needs neither.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, urlparse

import kit

#: Kinesis PutRecords takes at most 500 records; smaller batches keep a retry cheap.
PUT_BATCH = 100
#: The suffix the transcriber gives a later session's recording: ``_2026-11-28T17-21-24-123Z``.
SESSION_STAMP = re.compile(r"_\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z")
RETRYABLE = ("ProvisionedThroughputExceededException", "InternalFailure", "ServiceUnavailable")


# --- the stack -----------------------------------------------------------------


def _stack_resources(cfn: Any, stack_name: str) -> list[dict]:
    """Every resource of a stack; one page holds 100, and the AI stack has more."""
    return [
        r
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack_name)
        for r in page["StackResourceSummaries"]
    ]


@dataclass
class Stack:
    """What the kit needs from a deployed LMA stack."""

    name: str
    region: str
    outputs: dict[str, str]
    table: str
    bucket: str | None = None
    recordings_prefix: str = "lma-audio-recordings/"
    transcripts_prefix: str = "lma-transcripts/"
    session: Any = None

    @classmethod
    def resolve(cls, stack_name: str, region: str | None) -> "Stack":
        import boto3  # pylint: disable=import-outside-toplevel

        session = boto3.Session(region_name=region)
        cfn = session.client("cloudformation")
        root = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
        outputs = {o["OutputKey"]: o["OutputValue"] for o in root.get("Outputs", [])}
        ai_stack = next(
            (
                r["PhysicalResourceId"]
                for r in _stack_resources(cfn, stack_name)
                if r["LogicalResourceId"] == "AISTACK"
            ),
            None,
        )
        if not ai_stack:
            raise RuntimeError(f"{stack_name} has no AISTACK nested stack; is it an LMA stack?")
        table = next(
            r["PhysicalResourceId"]
            for r in _stack_resources(cfn, ai_stack)
            if r["LogicalResourceId"] == "EventSourcingTable"
        )
        params = {
            p["ParameterKey"]: p.get("ParameterValue", "")
            for p in cfn.describe_stacks(StackName=ai_stack)["Stacks"][0].get("Parameters", [])
        }
        return cls(
            name=stack_name,
            region=session.region_name,
            outputs=outputs,
            table=table,
            bucket=params.get("S3BucketName") or None,
            recordings_prefix=params.get("AudioFilePrefix") or "lma-audio-recordings/",
            transcripts_prefix=params.get("TranscriptFilePrefix") or "lma-transcripts/",
            session=session,
        )

    def client(self, service: str) -> Any:
        return self.session.client(service)

    @property
    def stream(self) -> str:
        return self.outputs["CallDataStreamName"]


def meeting_url(outputs: Mapping[str, str], call_id: str) -> str | None:
    """The meeting's page in the web UI: ``<ApplicationCloudfrontEndpoint>/#/calls/<CallId>``."""
    endpoint = outputs.get("ApplicationCloudfrontEndpoint")
    if not endpoint:
        return None
    if not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"
    return f"{endpoint.rstrip('/')}/#/calls/{quote(call_id, safe='')}"


def resolve_owner(cognito: Any, pool_id: str, username: str) -> str:
    """The Cognito username a sign-in name resolves to (an email alias becomes the pool's username)."""
    try:
        return cognito.admin_get_user(UserPoolId=pool_id, Username=username)["Username"]
    except Exception:  # noqa: BLE001 - the name as given is the best remaining answer
        return username


def user_pool_id(outputs: Mapping[str, str]) -> str:
    return (
        outputs.get("MCPServerUserPoolId")
        or outputs["CognitoUserPoolTokenIssuerUrl"].rsplit("/", 1)[-1]
    )


# --- Kinesis ---------------------------------------------------------------------


@dataclass
class KinesisSender:
    """Puts one meeting's records on the CallDataStream, partitioned by CallId (so in order)."""

    kinesis: Any
    stream: str
    call_id: str
    sleep: Callable[[float], None] = time.sleep
    sent: int = 0

    def put(self, record: Mapping[str, Any]) -> None:
        for attempt in range(6):
            try:
                self.kinesis.put_record(
                    StreamName=self.stream,
                    PartitionKey=self.call_id,
                    Data=json.dumps(record).encode(),
                )
                self.sent += 1
                return
            except Exception as err:  # noqa: BLE001
                code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
                if code not in RETRYABLE or attempt == 5:
                    raise
                self.sleep(0.2 * 2**attempt)

    def put_many(self, records: Sequence[Mapping[str, Any]]) -> None:
        """Batched, retrying only the records Kinesis reports as failed."""
        for offset in range(0, len(records), PUT_BATCH):
            pending = list(records[offset : offset + PUT_BATCH])
            for attempt in range(6):
                response = self.kinesis.put_records(
                    StreamName=self.stream,
                    Records=[
                        {"Data": json.dumps(r).encode(), "PartitionKey": self.call_id}
                        for r in pending
                    ],
                )
                failed = [r for r, out in zip(pending, response["Records"]) if out.get("ErrorCode")]
                self.sent += len(pending) - len(failed)
                if not failed:
                    break
                if attempt == 5:
                    raise RuntimeError(f"Kinesis refused {len(failed)} records after retries")
                pending = failed
                self.sleep(0.2 * 2**attempt)


# --- DynamoDB reads ------------------------------------------------------------


def plain(item: Mapping[str, Any]) -> dict[str, Any]:
    from boto3.dynamodb.types import TypeDeserializer  # pylint: disable=import-outside-toplevel

    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def get_meeting(ddb: Any, table: str, call_id: str) -> dict | None:
    key = {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}
    item = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
    return plain(item) if item else None


def query_partition(ddb: Any, table: str, pk: str) -> list[dict]:
    """Every item under one partition key, raw (attribute-value form), across pages."""
    items: list[dict] = []
    start = None
    while True:
        kwargs: dict[str, Any] = {
            "TableName": table,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
            "ConsistentRead": True,
        }
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = ddb.query(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def segments(ddb: Any, table: str, call_id: str) -> list[dict]:
    """The meeting's stored transcript segments and chat lines, in time order."""
    rows = [plain(r) for r in query_partition(ddb, table, f"trs#{call_id}")]
    rows = [r for r in rows if r.get("SegmentId") is not None]
    return sorted(rows, key=lambda r: (float(r.get("StartTime") or 0), str(r.get("SegmentId"))))


def wait_for(
    predicate: Callable[[], Any], timeout: float, interval: float = 3.0, sleep=time.sleep
) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        sleep(interval)


# --- cleanup -------------------------------------------------------------------


@dataclass
class CleanupReport:
    call_id: str
    rows: dict[str, int] = field(default_factory=dict)
    list_rows: int = 0
    objects: int = 0
    refused: str | None = None


def cleanup(
    ddb: Any,
    table: str,
    call_id: str,
    *,
    s3: Any = None,
    bucket: str | None = None,
    prefixes: Iterable[str] = (),
    force: bool = False,
) -> CleanupReport:
    """Delete one meeting's rows and files, and nothing else.

    The rows are every partition the meeting's own delete removes
    (kit.MEETING_PARTITION_PREFIXES), each queried by its exact key, and the
    meetings-list row named by the meeting's CreatedAt. Files are the objects whose
    key is one of ``prefixes`` followed by the CallId's file name and what the
    transcriber adds to it (see ``_is_own``), so another meeting whose id merely
    begins the same way is left alone. Refuses a meeting that is not the kit's (no
    ``synthetic-meeting`` label) unless ``force``.
    """
    report = CleanupReport(call_id)
    meeting = get_meeting(ddb, table, call_id)
    if meeting and not force:
        labels = {label.get("Key") for label in meeting.get("Labels") or []}
        if kit.LABEL_KEY not in labels:
            report.refused = (
                f"{call_id!r} has no {kit.LABEL_KEY!r} label, so the kit did not create it; "
                "pass --force to delete it anyway"
            )
            return report
    # The meeting row goes last: it holds the CreatedAt that names the list row and
    # the recording links, so a cleanup that stops part-way can be run again.
    if meeting and meeting.get("CreatedAt"):
        list_pk, list_sk = kit.list_row_key(call_id, str(meeting["CreatedAt"]))
        key = {"PK": {"S": list_pk}, "SK": {"S": list_sk}}
        if ddb.get_item(TableName=table, Key=key).get("Item"):
            ddb.delete_item(TableName=table, Key=key)
            report.list_rows += 1
    if s3 is not None and bucket:
        name = kit.posix_name(call_id)
        for prefix in prefixes:
            report.objects += _delete_objects(s3, bucket, f"{prefix}{name}")
        # A recording named otherwise is found through the link on the meeting row.
        for key in recording_keys(meeting or {}, bucket):
            report.objects += _delete_objects(s3, bucket, key, exact=True)
    partitions = kit.meeting_partitions(call_id)
    for pk in partitions[1:] + partitions[:1]:  # c#<CallId> is first in the list
        items = query_partition(ddb, table, pk)
        for item in items:
            ddb.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})
        if items:
            report.rows[pk.split("#", 1)[0] + "#"] = len(items)
    return report


def _is_own(rest: str, exact: bool) -> bool:
    """Whether a key that begins with the meeting's file name is the meeting's.

    ``<name>.wav``, ``<name>.raw``, ``<name>/...``, ``<name>-SUMMARY.txt`` and a
    later session's ``<name>_<timestamp>.wav`` (recording-upload.ts recordingStamp,
    an ISO time with ``:`` and ``.`` written as ``-``) are; ``<name>_copy.wav`` and
    ``<name>_2.wav`` are other meetings'.
    """
    if not rest:
        return True
    if exact:
        return False
    # A file name never holds "-" (it becomes "_"), so "<name>-SUMMARY.txt" is this
    # meeting's: the summary and transcript files the summarizer writes.
    return rest[0] in "./-" or bool(SESSION_STAMP.match(rest))


def _delete_objects(s3: Any, bucket: str, prefix: str, exact: bool = False) -> int:
    deleted = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
        for version in page.get("Versions", []) + page.get("DeleteMarkers", []):
            if not _is_own(version["Key"][len(prefix) :], exact):
                continue  # another object, or another meeting whose name begins with this one's
            s3.delete_object(Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"])
            deleted += 1
    return deleted


def recording_keys(meeting: Mapping[str, Any], bucket: str | None) -> list[str]:
    """Keys of this meeting's recordings in the stack's bucket, from the links on its row."""
    keys = []
    for url in [meeting.get("RecordingUrl"), *(meeting.get("RecordingUrls") or [])]:
        if not url:
            continue
        parsed = urlparse(str(url))
        if bucket and parsed.netloc.split(".", 1)[0] == bucket:
            keys.append(parsed.path.lstrip("/"))
    return sorted(set(keys))


# --- Amazon Polly ----------------------------------------------------------------


def synthesize(polly: Any, voice: str, text: str, cache_dir: Path | None = None) -> bytes:
    """16 kHz 16-bit mono PCM of one turn, cached on disk by voice and text."""
    cached = None
    if cache_dir is not None:
        digest = hashlib.sha256(f"{voice}\n{kit.SAMPLE_RATE}\n{text}".encode()).hexdigest()[:24]
        cached = cache_dir / f"{voice}-{digest}.pcm"
        if cached.exists():
            return cached.read_bytes()
    pcm = b"".join(
        polly.synthesize_speech(
            Text=piece,
            VoiceId=voice,
            Engine="neural",
            OutputFormat="pcm",
            SampleRate=str(kit.SAMPLE_RATE),
        )["AudioStream"].read()
        for piece in kit.split_for_polly(text)
    )
    if cached is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(pcm)
    return pcm


def synthesize_meeting(
    polly: Any, meeting: kit.Meeting, cache_dir: Path | None, progress=print
) -> dict[int, bytes]:
    pcm: dict[int, bytes] = {}
    turns = [(i, item) for i, item in enumerate(meeting.items) if item.kind == "turn"]
    for count, (index, item) in enumerate(turns, 1):
        pcm[index] = synthesize(polly, meeting.speaker(item.name).voice, item.text, cache_dir)
        if count % 10 == 0 or count == len(turns):
            progress(f"  synthesized {count}/{len(turns)} turns")
    return pcm


# --- the WebSocket transcriber ---------------------------------------------------


def mint_tokens(
    region: str, pool_id: str, client_id: str, username: str, password: str
) -> dict[str, str]:
    """Cognito tokens over SRP, which the app client allows (the same call as ws_stream_probe)."""
    from pycognito import Cognito  # pylint: disable=import-outside-toplevel

    user = Cognito(pool_id, client_id, username=username, user_pool_region=region)
    user.authenticate(password=password)
    return {"access": user.access_token, "id": user.id_token, "refresh": user.refresh_token}


def websocket_url(endpoint: str, tokens: Mapping[str, str]) -> str:
    return (
        f"{endpoint}?authorization=Bearer%20{tokens['access']}"
        f"&id_token={tokens['id']}&refresh_token={tokens['refresh']}"
    )


async def stream_meeting(
    connect: Callable[[str], Any],
    url: str,
    start_frame: Mapping[str, Any],
    chunks: Sequence[tuple[float, bytes]],
    speed: float,
    on_time: Callable[[float], Awaitable[None] | None] | None = None,
    send_end: bool = True,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Stream the audio at ``speed`` times real time and return the socket's final state.

    ``on_time(meeting_time)`` is called before each chunk, which is where the chat
    lines due by then are put on Kinesis. ``connect`` is ``websockets.connect`` (or
    a fake); it is used as an async context manager.
    """
    async with connect(url) as ws:
        await ws.send(json.dumps(start_frame))
        began = clock()
        for meeting_time, chunk in chunks:
            if on_time is not None:
                result = on_time(meeting_time)
                if asyncio.iscoroutine(result):
                    await result
            delay = began + kit.wall_offset(meeting_time, speed) - clock()
            if delay > 0:
                await sleep(delay)
            await ws.send(chunk)
        state = getattr(getattr(ws, "state", None), "name", "OPEN")
        if send_end:
            await ws.send(json.dumps(kit.websocket_end_frame(start_frame["callId"])))
            await sleep(1.0)  # let the END reach the server before the close
        return state
