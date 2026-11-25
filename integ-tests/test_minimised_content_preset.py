# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The Minimised content preset and its summary profile, against a live stack.

See "Minimised content" in docs/privacy-and-data-handling.md for the behaviour
asserted.

Default run (about five minutes, three model calls, self-cleaning):

``test_the_minimised_content_preset_is_a_managed_profile_with_its_values``
    The seeder wrote ``minimised-content`` as a Managed profile holding every
    value its preset sets, Meeting Assistant › Leave out sensitive personal
    information on among them. Read-only.
``test_every_profile_holds_the_minimised_content_summary_profile``
    The default profile and the Managed profile both hold the shipped
    ``minimised-content`` summary profile, whose every section carries the rule,
    so the default summary profile the preset names is one the deployment has.
    Read-only.
``test_a_summary_written_with_the_profile_leaves_out_a_health_condition``
    *Model-dependent* (marker ``model_dependent``). A synthetic meeting started
    with the ``minimised-content`` summary profile, through the call data stream
    as a client's START starts one, has a short project conversation in which one
    participant names a health condition and its treatment as the reason for an
    action item.
    At its end the summary is written with that profile's sections, and none of
    them names the condition, the treatment or a medical need. The meeting's active
    profile is not changed: the profile is chosen on the meeting itself, so the
    test needs no deployment-wide change. A failure here means the model did not
    follow the instruction on this run, which the preset's documentation says
    can happen; read the sections the failure prints before changing the
    prompts.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import pytest
from kds_pipeline_probe import cleanup
from live_meeting import read_meeting, wait_for
from meeting_consent_probe import Stack, _lma_config

PRESET_ID = "minimised-content"
MEETING_RECORD_TIMEOUT_S = 120.0
SUMMARY_TIMEOUT_S = 15 * 60
TRANSCRIPTS_PREFIX = "lma-transcripts/"
OWNER = "integ-test@lma.aws"

#: The rule's first words in each section of the summary profile.
SUMMARY_RULE = "Leave out sensitive personal information about anyone"


#: A short project meeting. One participant names a health condition and its
#: treatment, and it is the reason for an action item, so a summary written
#: without the instruction tends to report it: run directly against the summary
#: model with the shipped default prompts, 4 of 6 sections named a medical need;
#: with this profile's prompts, 0 of 10 did.
CONVERSATION = [
    ("s1", 0, 6, "Jordan Park",
     "Thanks for joining. Today we need to agree the launch date for the billing portal and sort out the desk moves."),
    ("s2", 7, 15, "Alex Rivera",
     ("Before the desk moves: I was diagnosed with type 1 diabetes last month, so I need to keep my insulin "
      "in a fridge near my desk.")),
    ("s3", 16, 23, "Jordan Park",
     "Of course. Facilities will put a small fridge at your new desk for the insulin before the move on Friday."),
    ("s4", 24, 29, "Jordan Park", "We decided to launch the billing portal on March 3."),
    ("s5", 30, 35, "Alex Rivera", "I will finish the payment provider integration by February 20."),
    ("s6", 36, 41, "Jordan Park", "The budget for the launch campaign stays at twenty thousand dollars."),
]
#: Words a summary that followed the instruction does not contain.
LEFT_OUT = ("diabet", "insulin", "hospital", "blood sugar", "medic", "health")
#: Words a summary of this meeting contains, so an empty or failed summary is not a pass.
KEPT = ("billing portal",)


@pytest.fixture(scope="module")
def stack(client) -> Stack:
    return Stack.find(client.stack_name, client.region)


def _store(stack: Stack):
    cfg = _lma_config()
    return cfg.ConfigurationStore(stack.configuration_table, client=stack.session.client("dynamodb"))


def _managed_profile(stack: Stack):
    record = _store(stack).read_profile(PRESET_ID, consistent_read=True)
    assert record.exists, f"the deployment has no Managed profile {PRESET_ID}; was it deployed from this branch?"
    assert record.metadata.get("Managed"), f"{PRESET_ID} is not a Managed profile"
    return record


def _holds(stored, value) -> bool:
    """Whether the stored value carries what the preset sets.

    A preset sets some keys of a nested setting (``meeting_assist.guidance``, say);
    the Managed profile is a full copy, so the stored object also holds the keys the
    preset leaves at their defaults.
    """
    if isinstance(value, dict):
        return isinstance(stored, dict) and all(_holds(stored.get(k), v) for k, v in value.items())
    return stored == value


# ── the Managed profile ────────────────────────────────────────────────────


def test_the_minimised_content_preset_is_a_managed_profile_with_its_values(stack: Stack) -> None:
    # _lma_config() puts the configuration layer on sys.path; import from it after.
    _lma_config()
    from lma_config.presets import preset_by_id  # pylint: disable=import-outside-toplevel

    record = _managed_profile(stack)
    for section, change in preset_by_id(PRESET_ID)["Sections"].items():
        stored = record.body.get(section) or {}
        for key, value in change["values"].items():
            assert _holds(stored.get(key), value), f"{PRESET_ID}: {section}.{key} is {stored.get(key)!r}"
    assert record.body["retention"]["transcriptDeletionAfterSummary"] == "immediately"
    assert record.body["retention"]["recordingDeletionAfterSummary"] == "immediately"
    assert record.body["recording"]["recordAudio"] is False
    assert record.body["recording"]["recordScreenVideo"] is False
    assert record.body["recording"]["recordVpMeetingVideo"] is False
    assert record.body["meeting_assist"]["omitSensitivePersonalInformation"] is True
    assert record.body["meeting_processing"]["defaultSummaryProfile"] == PRESET_ID


def test_every_profile_holds_the_minimised_content_summary_profile(stack: Stack) -> None:
    store = _store(stack)
    for name in ("default", PRESET_ID):
        record = store.read_profile(name, consistent_read=True)
        assert record.exists, name
        profile = (record.body.get("summary_profiles") or {}).get(PRESET_ID)
        assert profile, f"the {name} profile has no {PRESET_ID} summary profile"
        prompts = profile.get("prompts") or {}
        assert prompts, f"{name}: the {PRESET_ID} summary profile has no sections"
        for title, prompt in prompts.items():
            assert SUMMARY_RULE in prompt, f"{name}: {title} does not carry the rule"


# ── a summary written with the profile ─────────────────────────────────────


def _posix(name: str) -> str:
    """A file name as bedrock_summary_lambda posixify_filename makes it."""
    return re.sub(r"_+$", "", re.sub(r"^_+", "", re.sub(r"[^a-zA-Z0-9_.]", "_", name)))


def _list_key(created_at: str, call_id: str) -> dict[str, Any]:
    """The key of a meeting's list entry, as createCall writes it (a four-hour shard of the day)."""
    shard = int(created_at[11:13]) // 4
    return {
        "PK": {"S": f"cls#{created_at[:10]}#s#{shard:02d}"},
        "SK": {"S": f"ts#{created_at}#id#{call_id}"},
    }


def _segment(call_id: str, segment_id: str, start: float, end: float, speaker: str, text: str) -> dict:
    return {
        "PK": {"S": f"trs#{call_id}"},
        "SK": {"S": f"ts#{start:09.3f}#{segment_id}"},
        "CallId": {"S": call_id},
        "SegmentId": {"S": segment_id},
        "StartTime": {"N": str(start)},
        "EndTime": {"N": str(end)},
        "Transcript": {"S": text},
        "Channel": {"S": "REMOTE"},
        "Speaker": {"S": speaker},
        "IsPartial": {"BOOL": False},
        "CreatedAt": {"S": datetime.now(timezone.utc).isoformat()},
        "Owner": {"S": OWNER},
        "ExpiresAfter": {"N": str(int(time.time()) + 86400)},
    }


@pytest.fixture(scope="module")
def summarised_meeting(client, stack: Stack):
    """A synthetic meeting summarized with the minimised-content profile; removed after."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    call_id = f"integ-minimised-content-{stamp}"
    created_at = datetime.now(timezone.utc).isoformat()
    kinesis = stack.session.client("kinesis")
    ddb = stack.session.client("dynamodb")
    s3 = stack.session.client("s3")
    stream = stack.outputs["CallDataStreamName"]
    key = {"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}}

    def put(record: dict) -> None:
        kinesis.put_record(StreamName=stream, Data=json.dumps(record).encode(), PartitionKey=call_id)

    try:
        put({
            "EventType": "START",
            "CallId": call_id,
            "CustomerPhoneNumber": "Integ Test Participant",
            "SystemPhoneNumber": "LMA Integ Test",
            # With no access token the processor makes AgentId the Owner.
            "AgentId": OWNER,
            "CreatedAt": created_at,
            "AccessToken": "",
            "IdToken": "",
            "RefreshToken": "",
            "SummaryProfile": PRESET_ID,
        })
        meeting = wait_for(lambda: read_meeting(ddb, stack.event_table, call_id),
                           timeout=MEETING_RECORD_TIMEOUT_S, interval=3.0)
        assert meeting, f"the START for {call_id} did not create a meeting"
        rows = [_segment(call_id, *line) for line in CONVERSATION]
        ddb.batch_write_item(RequestItems={stack.event_table: [{"PutRequest": {"Item": r}} for r in rows]})
        # The segments are written to the table directly, so the meeting gets the
        # duration the call event processor's aggregation would have given it.
        ddb.update_item(
            TableName=stack.event_table,
            Key=key,
            UpdateExpression="SET TotalConversationDurationMillis = :d",
            ExpressionAttributeValues={":d": {"N": "41000"}},
        )
        put({"EventType": "END", "CallId": call_id, "UpdatedAt": datetime.now(timezone.utc).isoformat()})

        def summary_state() -> dict | None:
            row = ddb.get_item(TableName=stack.event_table, Key=key, ConsistentRead=True).get("Item") or {}
            raw = row.get("CallSummaryState", {}).get("S")
            state = json.loads(raw) if raw else {}
            return {"state": state, "row": row} if state.get("status") in ("idle", "failed", "empty") else None

        done = wait_for(summary_state, timeout=SUMMARY_TIMEOUT_S, interval=10.0)
        assert done, "no end-of-meeting summary within the timeout"
        yield done
    finally:
        cleanup(client.stack_name, call_id, region=client.region)
        with contextlib.suppress(Exception):
            ddb.delete_item(TableName=stack.event_table, Key=_list_key(created_at, call_id))
        for segment_id, start, *_rest in CONVERSATION:
            with contextlib.suppress(Exception):
                ddb.delete_item(
                    TableName=stack.event_table,
                    Key={"PK": {"S": f"trs#{call_id}"}, "SK": {"S": f"ts#{start:09.3f}#{segment_id}"}},
                )
        with contextlib.suppress(Exception):
            listed = s3.list_objects_v2(Bucket=stack.recordings_bucket, Prefix=f"{TRANSCRIPTS_PREFIX}{_posix(call_id)}")
            for entry in listed.get("Contents", []):
                s3.delete_object(Bucket=stack.recordings_bucket, Key=entry["Key"])


@pytest.mark.model_dependent
def test_a_summary_written_with_the_profile_leaves_out_a_health_condition(summarised_meeting) -> None:
    state, row = summarised_meeting["state"], summarised_meeting["row"]
    assert state["status"] == "idle", state
    assert state.get("profile") == PRESET_ID, state
    sections = json.loads(row.get("CallSummaryText", {}).get("S") or "{}")
    assert {"SUMMARY", "ACTIONS"} <= set(sections), sections
    # No agenda, so no agenda coverage.
    assert "AGENDA COVERAGE" not in sections
    text = "\n".join(str(value) for value in sections.values()).casefold()
    for kept in KEPT:
        assert kept in text, f"the summary does not mention {kept!r}: {sections}"
    leaked = [word for word in LEFT_OUT if word in text]
    assert not leaked, (
        f"model-dependent: the summary names {leaked}, which the profile's instruction asks it to "
        f"leave out (see docs/privacy-and-data-handling.md#minimised-content): {sections}"
    )
