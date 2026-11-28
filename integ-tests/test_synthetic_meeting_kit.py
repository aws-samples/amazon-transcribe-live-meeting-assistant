# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The synthetic meeting kit (scripts/synthetic_meetings) against a live stack.

Replays the bundled ``retrospective`` meeting through Kinesis at fast pace with
the kit's own command line, leaving it live (no END, so no summary is
generated), and checks what the stack stored: the meeting row with the script's
title, agenda, meeting context and label; one final segment per turn with the
script's speaker, channel, text and times; and the chat message on MEETING_CHAT.
Then removes the meeting with the kit's ``--cleanup`` and checks that nothing of
it is left.

Needs only AWS credentials: no Cognito user, no model call.
"""

from __future__ import annotations

import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1] / "scripts" / "synthetic_meetings"
sys.path.insert(0, str(KIT))

import delivery  # noqa: E402
import kit  # noqa: E402
import run_meeting  # noqa: E402

MEETING = "retrospective"
OWNER = "integ-test@lma.aws"
STORE_WAIT_SECONDS = 90


@pytest.fixture(scope="module")
def stack(client) -> delivery.Stack:
    return delivery.Stack.resolve(client.stack_name, client.region)


def test_synthetic_meeting_replays_through_kinesis_and_cleans_up(
    stack: delivery.Stack, client
) -> None:
    meeting = kit.find_meeting(MEETING)
    call_id = kit.default_call_id(meeting, datetime.now(timezone.utc), f"it{secrets.token_hex(2)}")
    ddb = stack.client("dynamodb")
    expected = {kit.segment_id(call_id, t): t for t in kit.build_timeline(meeting)}
    cleanup_args = ["--stack", client.stack_name, "--cleanup", call_id]
    if client.region:
        cleanup_args += ["--region", client.region]
    try:
        args = [
            "--stack",
            client.stack_name,
            "--meeting",
            MEETING,
            "--owner",
            OWNER,
            "--call-id",
            call_id,
        ]
        args += ["--no-end", "--no-verify"] + (["--region", client.region] if client.region else [])
        assert run_meeting.main(args) == 0

        row = delivery.get_meeting(ddb, stack.table, call_id)
        assert row, f"no meeting record for {call_id!r}"
        assert row["CallId"].startswith(meeting.title), "the meetings list shows the script's title"
        assert row["Agenda"] == meeting.agenda
        assert row["MeetingContext"] == meeting.meeting_context
        assert row["Owner"] == OWNER
        assert {"Key": kit.LABEL_KEY, "Value": MEETING} in (row.get("Labels") or [])
        assert row["Status"] != "ENDED", "--no-end leaves the meeting live"

        def all_stored() -> list[dict] | None:
            rows = delivery.segments(ddb, stack.table, call_id)
            return rows if len(rows) >= len(expected) else None

        rows = delivery.wait_for(all_stored, STORE_WAIT_SECONDS) or delivery.segments(
            ddb, stack.table, call_id
        )
        by_id = {row["SegmentId"]: row for row in rows}
        missing = sorted(set(expected) - set(by_id))
        assert not missing, (
            f"{len(missing)} of {len(expected)} segments not stored within {STORE_WAIT_SECONDS}s"
        )
        for segment_id, item in expected.items():
            row = by_id[segment_id]
            assert row["Speaker"] == item.name, segment_id
            assert row["Channel"] == item.channel, segment_id
            assert row["Transcript"] == item.text, segment_id
            assert float(row["StartTime"]) == pytest.approx(item.start, abs=0.01), segment_id
            assert float(row["EndTime"]) == pytest.approx(item.end, abs=0.01), segment_id
            assert row["IsPartial"] is False
        chats = [row for row in rows if row["Channel"] == kit.CHAT_CHANNEL]
        assert [(c["Speaker"], c["Transcript"]) for c in chats] == [
            (c.name, c.text) for c in meeting.chats
        ]
    finally:
        cleaned = run_meeting.main(cleanup_args)

    assert cleaned == 0, "the kit refused to clean up its own meeting"
    assert delivery.get_meeting(ddb, stack.table, call_id) is None
    for pk in kit.meeting_partitions(call_id):
        assert not delivery.query_partition(ddb, stack.table, pk), f"{pk} survived the cleanup"
