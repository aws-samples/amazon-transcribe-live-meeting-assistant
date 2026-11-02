# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Meeting consent over the Stream Audio socket, end to end.

The pieces ``test_meeting_consent.py`` is built from:

``ConsentSettingsOverride``
    Sets the deployment's Meeting Consent mode and rule for the length of a
    test and puts the section back afterwards. It writes the active
    configuration profile through the ``lma_config`` store with the caller's
    IAM credentials, the same store and the same profile the Configuration
    page's save goes through (``updateConfiguration``), so the change is
    recorded in the configuration history like any other save. That page's
    mutation is open only to the Admin group, and the integration test user is
    not an Admin, which is why the test does not use it. The transcriber reads
    the section on the immediate tier (no cache), so a write takes effect on
    the next ``START``.

``stream_meeting``
    One Stream Audio session as the Cognito test user, using the same
    ``START`` / binary PCM / ``END`` protocol as ``ws_stream_probe.py``, with a
    ``consent`` step in ``START``, an optional ``CONSENT_EVIDENCE`` image sent
    straight after it, and an optional ``CONSENT_OBJECTION`` part-way through.
    Every frame the server sends back is collected, with the close code.

``read_meeting`` / ``segments``
    What was stored: the meeting row with its ``Consent`` record, and the
    transcript segments.

The protocol is described in ``docs/websocket-streaming-api.md`` (section
6.1.5) and the behaviour in ``docs/meeting-consent.md``.

Depends on ``pycognito`` and ``websockets`` (``requirements.txt``) and on
``pillow`` (``requirements-video.txt``) for the evidence image.

Standalone, to print what a deployment answers without changing anything:
    LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user AWS_PROFILE=default \\
      python integ-tests/meeting_consent_probe.py lma-integtest1 --region us-west-2
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import cognito_test_user
from ws_stream_probe import _DEFAULT_WAV, _mint_tokens, _outputs, _stack_resources

_CONFIGURATION_LAYER = (
    Path(__file__).resolve().parent.parent
    / "lma-ai-stack/source/lambda_layers/configuration_layer"
)

SECTION = "meeting_consent"
UPDATED_BY = "integ-tests/test_meeting_consent.py"
CHUNK_MS = 200
# How long a deployment's transcriber has to answer CONSENT_SETTINGS. The
# desktop app allows six seconds before deciding the transcriber predates the
# consent frames; a little more here, since a test run is not interactive.
SETTINGS_TIMEOUT_SECONDS = 10.0
EVIDENCE_PREFIX = "lma-consent-evidence/"


def _lma_config():
    """The configuration layer's store, imported from this repository's source."""
    if str(_CONFIGURATION_LAYER) not in sys.path:
        sys.path.insert(0, str(_CONFIGURATION_LAYER))
    import lma_config

    return lma_config


# ── the stack ──────────────────────────────────────────────────────────────


@dataclass
class Stack:
    """The resources of one deployment that the consent test reads or writes."""

    name: str
    region: str
    session: Any
    outputs: dict
    event_table: str
    configuration_table: str
    configuration_bucket: str
    recordings_bucket: str
    # Logical ids of the AI nested stack's resources.
    ai_resources: frozenset = frozenset()

    @classmethod
    def find(cls, stack_name: str, region: str | None = None) -> Stack:
        session = boto3.Session(region_name=region)
        cfn = session.client("cloudformation")
        outputs = _outputs(cfn, stack_name)
        ai_stack = next(
            (r["PhysicalResourceId"] for r in _stack_resources(cfn, stack_name)
             if r["LogicalResourceId"] == "AISTACK"),
            None,
        )
        if not ai_stack:
            raise RuntimeError(f"{stack_name} has no AISTACK nested stack")
        ai = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, ai_stack)}
        missing = [n for n in ("EventSourcingTable", "ConfigurationTable", "ConfigurationBucket") if n not in ai]
        if missing:
            raise RuntimeError(f"{ai_stack} has no {', '.join(missing)}")
        if not outputs.get("RecordingsS3Bucket"):
            raise RuntimeError(f"{stack_name} has no RecordingsS3Bucket output")
        return cls(
            name=stack_name,
            region=session.region_name,
            session=session,
            outputs=outputs,
            event_table=ai["EventSourcingTable"],
            configuration_table=ai["ConfigurationTable"],
            configuration_bucket=ai["ConfigurationBucket"],
            recordings_bucket=outputs["RecordingsS3Bucket"],
            ai_resources=frozenset(ai),
        )

    def has_consent_api(self) -> bool:
        """Whether the deployment has the meeting consent resolver at all."""
        return "MeetingConsentResolverFunction" in self.ai_resources


# ── the configuration ─────────────────────────────────────────────────────


class ConsentSettingsOverride:
    """Change the Meeting Consent section of the active profile, and put it back.

    ``save`` reads the section as the active profile holds it (possibly not at
    all, when the profile leaves the whole section to the deployment's
    baseline) before anything is written. ``apply`` replaces it with the saved
    values plus the given overrides. ``restore`` puts back exactly what was
    saved: the saved values, or no section at all if there was none. It reads
    the profile again and changes only this section, so a save made to another
    section while the test ran is kept, and it is safe to call more than once.
    """

    def __init__(self, stack: Stack | None = None, *, store: Any = None,
                 section: str = SECTION, updated_by: str = UPDATED_BY) -> None:
        cfg = _lma_config()
        self._cfg = cfg
        # Another section for another test (test_transcript_deletion.py uses
        # Retention); the same save, apply and restore.
        self.section = section
        self.updated_by = updated_by
        # `store` is for the plumbing tests, which run without AWS.
        self.store = store or cfg.ConfigurationStore(
            stack.configuration_table,
            client=stack.session.client("dynamodb"),
            # Given the bucket, the store records each save in the
            # configuration history, as the Configuration page's save does.
            revision_bucket=stack.configuration_bucket,
            s3_client=stack.session.client("s3"),
        )
        self.profile: str | None = None
        self.had_section = False
        self.saved: dict | None = None
        self.applied = False

    def save(self) -> None:
        active = self.store.active_profile_name(consistent_read=True)
        if not active:
            raise RuntimeError(
                "the deployment has no active configuration profile, so a setting saved by "
                "this test would not take effect; the configuration seeder sets one"
            )
        record = self.store.read_profile(active, consistent_read=True)
        if not record.exists:
            raise RuntimeError(f"the active configuration profile {active!r} is not stored")
        self.profile = active
        self.had_section = self.section in record.body
        self.saved = dict(record.body.get(self.section) or {}) if self.had_section else None

    def apply(self, **overrides: Any) -> None:
        if self.profile is None:
            raise RuntimeError("save() must run before apply()")
        values = {**(self.saved or {}), **overrides}
        self.store.write_section(
            self.profile, self.section, values, updated_by=self.updated_by, expected_version=None,
            comment=f"integration test: {self.section} {overrides}",
        )
        self.applied = True

    def resolved(self) -> dict:
        """The section as the transcriber resolves it: baseline merged with the profile."""
        self.store.invalidate()
        return dict(self.store.get_section(self.section).values)

    def restore(self, attempts: int = 5) -> None:
        if self.profile is None or not self.applied:
            return
        for attempt in range(attempts):
            current = self.store.read_profile(self.profile, consistent_read=True)
            body = dict(current.body)
            if self.had_section:
                body[self.section] = dict(self.saved or {})
            else:
                body.pop(self.section, None)
            try:
                self.store.write_profile(
                    self.profile, body, updated_by=self.updated_by, expected_version=current.version,
                    comment=f"integration test: {self.section} restored",
                )
                self.applied = False
                return
            except self._cfg.ConcurrentModification:
                if attempt == attempts - 1:
                    raise
                time.sleep(1.0)


# ── the evidence image ─────────────────────────────────────────────────────


def evidence_jpeg(notice: str, reply_name: str = "Bo Chen", reply: str = "I consent") -> bytes:
    """A synthetic meeting chat: the consent request, then one participant's reply."""
    import textwrap

    from PIL import Image, ImageDraw, ImageFont

    width, height = 1280, 720
    image = Image.new("RGB", (width, height), (245, 246, 248))
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=34)
    body = ImageFont.load_default(size=26)
    draw.rectangle((0, 0, width, 70), fill=(40, 52, 120))
    draw.text((30, 16), "Meeting chat", font=title, fill=(255, 255, 255))
    y = 100
    draw.text((40, y), "Integration Test (organizer)", font=body, fill=(40, 52, 120))
    y += 40
    for line in textwrap.wrap(notice, 80):
        draw.text((60, y), line, font=body, fill=(20, 20, 20))
        y += 34
    y += 30
    draw.text((40, y), reply_name, font=body, fill=(40, 52, 120))
    draw.text((60, y + 40), reply, font=body, fill=(20, 20, 20))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


# ── streaming ──────────────────────────────────────────────────────────────


def tokens_for_test_user(stack: Stack) -> tuple[str, dict]:
    credentials = cognito_test_user.resolve(stack.region)
    if credentials is None:
        raise cognito_test_user.CredentialsUnavailable(
            f"no Cognito test user configured — {cognito_test_user.describe_sources()}"
        )
    username, password = credentials
    pool_id = stack.outputs.get("MCPServerUserPoolId") or stack.outputs.get(
        "CognitoUserPoolTokenIssuerUrl", "").rsplit("/", 1)[-1]
    tokens = _mint_tokens(stack.region, pool_id, stack.outputs["CognitoUserPoolClientId"], username, password)
    return username, tokens


def _ws_url(stack: Stack, tokens: dict) -> str:
    return (
        f"{stack.outputs['LMAWebsocketEndpoint']}?authorization=Bearer%20{tokens['access']}"
        f"&id_token={tokens['id']}&refresh_token={tokens['refresh']}"
    )


def new_call_id(label: str) -> str:
    return f"consent-probe-{label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"


def evidence_folder(call_id: str) -> str:
    """The meeting's evidence folder (docs/meeting-consent.md, 'What is stored')."""
    return EVIDENCE_PREFIX + re.sub(r"[^A-Za-z0-9_.]", "_", call_id).strip("_") + "/"


async def _consent_settings(url: str) -> dict | None:
    import websockets

    async with websockets.connect(url, max_size=None, open_timeout=25) as ws:
        await ws.send(json.dumps({"callEvent": "CONSENT_SETTINGS"}))
        deadline = time.monotonic() + SETTINGS_TIMEOUT_SECONDS
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(ws.recv(), remaining)
            except asyncio.TimeoutError:
                return None
            if isinstance(raw, str):
                frame = json.loads(raw)
                if frame.get("event") == "CONSENT_SETTINGS":
                    return frame
    return None


def consent_settings(stack: Stack, tokens: dict) -> dict | None:
    """The transcriber's CONSENT_SETTINGS answer, or None if it gives none."""
    return asyncio.run(_consent_settings(_ws_url(stack, tokens)))


@dataclass
class Session:
    """What happened on one streaming connection."""

    call_id: str
    frames: list = field(default_factory=list)
    close_code: int | None = None
    close_reason: str = ""
    seconds_sent: float = 0.0
    closed_by_server: bool = False
    objection_sent_at: float | None = None
    closed_at: float | None = None
    end_sent: bool = False

    def consent_states(self) -> list:
        return [f for f in self.frames if f.get("event") == "CONSENT_STATE"]

    def codes(self) -> list:
        return [f["code"] for f in self.consent_states() if f.get("code")]

    def summary(self) -> str:
        states = [
            {k: f.get(k) for k in ("status", "captureAllowed", "code", "evidence") if f.get(k) is not None}
            for f in self.consent_states()
        ]
        return (f"call {self.call_id}: sent {self.seconds_sent:.1f}s of audio, close code "
                f"{self.close_code} {self.close_reason!r}, CONSENT_STATE frames {states}")


def _pcm_chunks(wav_path: Path, seconds: float) -> tuple[int, list[bytes]]:
    with wave.open(str(wav_path), "rb") as wav:
        rate = wav.getframerate()
        frames_per_chunk = int(rate * CHUNK_MS / 1000)
        chunks = []
        for _ in range(int(seconds * 1000 / CHUNK_MS)):
            data = wav.readframes(frames_per_chunk)
            if not data:
                break
            chunks.append(data)
    return rate, chunks


async def _stream(url: str, start: dict, chunks: list[bytes], evidence: dict | None,
                  objection: dict | None, objection_after_seconds: float | None,
                  linger_seconds: float) -> Session:
    import websockets

    session = Session(call_id=start["callId"])
    async with websockets.connect(url, max_size=None, open_timeout=25) as ws:
        async def receive() -> None:
            try:
                async for raw in ws:
                    if isinstance(raw, str):
                        try:
                            session.frames.append(json.loads(raw))
                        except ValueError:
                            session.frames.append({"unparsed": raw})
            except websockets.ConnectionClosed:
                pass
            session.closed_at = time.monotonic()

        reader = asyncio.create_task(receive())
        await ws.send(json.dumps(start))
        if evidence:
            # Straight after START, as Stream Audio does with an image taken
            # before Start: the server holds it until the meeting is registered.
            await ws.send(json.dumps({"callEvent": "CONSENT_EVIDENCE", **evidence}))
        t0 = time.monotonic()
        try:
            for i, chunk in enumerate(chunks):
                if reader.done():
                    session.closed_by_server = True
                    break
                if (objection is not None and session.objection_sent_at is None
                        and i * CHUNK_MS / 1000 >= (objection_after_seconds or 0)):
                    await ws.send(json.dumps({"callEvent": "CONSENT_OBJECTION", **objection}))
                    session.objection_sent_at = time.monotonic()
                await ws.send(chunk)
                session.seconds_sent = (i + 1) * CHUNK_MS / 1000
                # Real time: the opt-in gate opens a second or two in, and the
                # objection must land while audio is still arriving.
                delay = t0 + (i + 1) * CHUNK_MS / 1000 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            if not reader.done():
                await ws.send(json.dumps({"callEvent": "END", "callId": start["callId"]}))
                session.end_sent = True
        except websockets.ConnectionClosed:
            session.closed_by_server = True
        # Long enough for the evidence check and the last consent frames.
        try:
            await asyncio.wait_for(asyncio.shield(reader), linger_seconds)
        except asyncio.TimeoutError:
            await ws.close()
            await reader
        session.close_code = ws.close_code
        session.close_reason = ws.close_reason or ""
    return session


def stream_meeting(stack: Stack, tokens: dict, username: str, *, label: str, consent: dict | None,
                   seconds: float = 20.0, evidence: dict | None = None, objection: dict | None = None,
                   objection_after_seconds: float | None = None, linger_seconds: float = 15.0,
                   wav_path: Path | None = None) -> Session:
    """Stream one meeting with the given consent step. See the module docstring."""
    call_id = new_call_id(label)
    rate, chunks = _pcm_chunks(Path(wav_path or _DEFAULT_WAV), seconds)
    # No `channels` field: see ws_stream_probe._stream_wav.
    start = {
        "callEvent": "START", "callId": call_id,
        "fromNumber": "Consent Probe", "toNumber": "LMA System",
        "agentId": username, "samplingRate": rate,
    }
    if consent is not None:
        start["consent"] = consent
    return asyncio.run(_stream(_ws_url(stack, tokens), start, chunks, evidence, objection,
                               objection_after_seconds, linger_seconds))


# ── what was stored ────────────────────────────────────────────────────────


def _plain(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    return value


def read_meeting(stack: Stack, call_id: str) -> dict | None:
    from boto3.dynamodb.types import TypeDeserializer

    pk = f"c#{call_id}"
    item = stack.session.client("dynamodb").get_item(
        TableName=stack.event_table, Key={"PK": {"S": pk}, "SK": {"S": pk}}, ConsistentRead=True,
    ).get("Item")
    if not item:
        return None
    d = TypeDeserializer()
    return _plain({k: d.deserialize(v) for k, v in item.items()})


def segments(stack: Stack, call_id: str) -> list:
    from boto3.dynamodb.types import TypeDeserializer

    ddb = stack.session.client("dynamodb")
    d = TypeDeserializer()
    kwargs = {
        "TableName": stack.event_table,
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": f"trs#{call_id}"}},
        "ConsistentRead": True,
    }
    items: list = []
    while True:
        resp = ddb.query(**kwargs)
        items += [_plain({k: d.deserialize(v) for k, v in i.items()}) for i in resp.get("Items", [])]
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def wait_for(predicate, timeout: float, interval: float = 3.0):
    """Poll ``predicate`` until it returns something truthy, or the time runs out."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def object_exists(stack: Stack, key: str) -> dict | None:
    """The object's size and type, or None when there is no such object.

    Any other error (credentials, permissions) is raised, so that it is not
    reported as a missing image.
    """
    from botocore.exceptions import ClientError

    try:
        head = stack.session.client("s3").head_object(Bucket=stack.recordings_bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return {"bytes": head.get("ContentLength", 0), "content_type": head.get("ContentType")}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("stack")
    parser.add_argument("--region")
    args = parser.parse_args(argv)
    stack = Stack.find(args.stack, args.region)
    override = ConsentSettingsOverride(stack)
    override.save()
    print(f"active profile: {override.profile}; holds a {SECTION} section: {override.had_section}")
    print(f"resolved {SECTION}: {json.dumps(override.resolved(), indent=1)}")
    _, tokens = tokens_for_test_user(stack)
    print(f"CONSENT_SETTINGS answer: {consent_settings(stack, tokens)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
