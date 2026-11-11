# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A Virtual Participant in a real Google Meet meeting, end to end.

The other tests reach the transcript by putting events on the stack's stream or
by streaming audio to the WebSocket server. This one goes through a real
meeting platform:

1. a scripted guest (``scripts/meeting_guest``) joins a Google Meet meeting
   from the stack's own VP image;
2. a Virtual Participant is sent to the same meeting, and the guest plays a
   two-voice conversation into its microphone;
3. the guest sends a chat message with a marker in it;
4. the meeting's transcript gets speech segments, and the chat message on the
   MEETING_CHAT channel.

It needs a meeting that admits anonymous guests without a host present: a
Google Meet with open access, kept on a recurring calendar event so its link
never expires. Set ``LMA_TEST_MEET_URL`` to the link; without it the test skips.

Google Meet decides for itself whether to admit an anonymous guest, and it now
and then turns one away whatever the guest does. When it turns the scripted
guest away after its retries, the test skips (``external_meeting``) rather than
failing the run, because that says nothing about LMA. The Virtual Participant's
own join is LMA's code path and is asserted.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer

from kds_pipeline_probe import _stack_resources
from lma_sdk import LMAClient

ROOT = Path(__file__).resolve().parents[1]
# Guest tasks to try before reporting that Meet would not admit one.
GUEST_ATTEMPTS = 3
# Virtual Participants to send before reporting that none could join.
VP_ATTEMPTS = 2
# Silence before the guest speaks: its own join, then up to two VP launches and joins.
GUEST_LEAD_IN_S = 540.0


def _load_meeting_guest():
    spec = importlib.util.spec_from_file_location(
        "meeting_guest", ROOT / "scripts" / "meeting_guest" / "meeting_guest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["meeting_guest"] = module
    spec.loader.exec_module(module)
    return module


def _wait_for(predicate: Callable[[], Any], timeout: float, interval: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def _transcript_items(
    session: boto3.session.Session, table: str, call_id: str
) -> List[Dict[str, Any]]:
    ddb = session.client("dynamodb")
    d = TypeDeserializer()
    kwargs: Dict[str, Any] = {
        "TableName": table,
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": f"trs#{call_id}"}},
    }
    items: List[Dict[str, Any]] = []
    while True:
        resp = ddb.query(**kwargs)
        items += [{k: d.deserialize(v) for k, v in i.items()} for i in resp.get("Items", [])]
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _event_table(session: boto3.session.Session, stack_name: str) -> str:
    cfn = session.client("cloudformation")
    ai_stack = next(
        r["PhysicalResourceId"]
        for r in _stack_resources(cfn, stack_name)
        if r["LogicalResourceId"] == "AISTACK"
    )
    return next(
        r["PhysicalResourceId"]
        for r in _stack_resources(cfn, ai_stack)
        if r["LogicalResourceId"] == "EventSourcingTable"
    )


@pytest.mark.external_meeting
def test_vp_transcribes_speech_and_chat_from_a_google_meet(
    client: LMAClient, stack_name: str, region: Optional[str]
) -> None:
    meet_url = os.environ.get("LMA_TEST_MEET_URL", "").strip()
    if not meet_url:
        pytest.skip("LMA_TEST_MEET_URL is not set; no meeting to join")

    guest_tool = _load_meeting_guest()
    session = boto3.session.Session(region_name=region) if region else boto3.session.Session()
    table = _event_table(session, stack_name)
    marker = f"integ meet marker {datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    vp_id: Optional[str] = None
    guest = None
    try:
        # The guest first: a Virtual Participant leaves a meeting it is alone in
        # after about a minute. The guest's audio plays from the moment its
        # microphone opens on the pre-join screen, so its conversation starts
        # after a lead-in long enough for the VP to join too.
        outcome = "not started"
        for _ in range(GUEST_ATTEMPTS):
            # A fresh task has a new address and browser fingerprint, which Meet
            # often admits when it turned the previous one away.
            guest = guest_tool.start_guest(
                session,
                stack_name,
                meet_url,
                name="Integ Test Guest",
                max_minutes=25,
                lead_in_s=GUEST_LEAD_IN_S,
            )
            outcome = guest_tool.wait_until_joined(session, guest, timeout_s=600)
            if outcome != "gave_up":
                break
            guest_tool.stop_guest(session, guest, leave_first=False)
            guest = None
        if outcome == "gave_up":
            pytest.skip(f"Google Meet turned the scripted guest away {GUEST_ATTEMPTS} times")
        assert outcome == "in_call", f"the scripted guest did not join the meeting ({outcome})"

        # Meet now and then refuses a participant at its Join click, which the
        # VP reports by ending without joining. A second VP shows whether that
        # was Meet's passing refusal or LMA's join failing.
        meeting: Optional[str] = None
        for _ in range(VP_ATTEMPTS):
            launch = client.vp.create(
                meeting_name="integ-test Google Meet end to end",
                platform="GOOGLE_MEET",
                meeting_id=meet_url,
                user_name="LMA Integ Test",
                wait=True,
                timeout_s=300.0,
            )
            vp_id = launch.id
            assert launch.status != "FAILED", f"VP launch failed: sfn_status={launch.sfn_status}"

            def joined() -> Optional[str]:
                row = client.vp.get(vp_id)
                if row.status == "ACTIVE":
                    return "joined"
                if row.status in ("ENDED", "COMPLETED", "FAILED"):
                    return f"left without joining ({row.status})"
                return None

            state = _wait_for(joined, timeout=420)
            if state == "joined":
                meeting = client.vp.get(vp_id).CallId
                break
            client.vp.end(vp_id, reason="integ-test Google Meet retry")
            vp_id = None
        assert meeting, (
            f"the Virtual Participant did not join the meeting in {VP_ATTEMPTS} attempts "
            f"(last: {state or 'still not ACTIVE after 7 minutes'})"
        )

        words = ("priya", "marcus", "launch", "agenda", "march", "sync", "app store", "submit")

        def speech() -> str:
            said = " ".join(
                (i.get("Transcript") or "")
                for i in _transcript_items(session, table, meeting)
                if i.get("Channel") != "MEETING_CHAT" and not i.get("IsPartial")
            ).lower()
            return said if any(word in said for word in words) else ""

        def chat() -> List[Dict[str, Any]]:
            return [
                i
                for i in _transcript_items(session, table, meeting)
                if i.get("Channel") == "MEETING_CHAT" and marker in (i.get("Transcript") or "")
            ]

        said = _wait_for(speech, timeout=GUEST_LEAD_IN_S + 300)
        assert said, "the guest's conversation did not reach the meeting's transcript"
        # Only now, with the VP transcribing, is it surely reading the meeting
        # chat; Meet shows a participant only messages sent after they joined.
        messages: List[Dict[str, Any]] = []
        for _ in range(2):
            guest_tool.send_command(session, guest, f"chat {marker}")
            messages = _wait_for(chat, timeout=120)
            if messages:
                break
        assert messages, "the guest's chat message did not reach the transcript"
    finally:
        if guest is not None:
            try:
                guest_tool.stop_guest(session, guest)
            except Exception as exc:  # noqa: BLE001
                print(f"guest cleanup failed: {exc}")
        if vp_id:
            client.vp.end(vp_id, reason="integ-test Google Meet cleanup")
