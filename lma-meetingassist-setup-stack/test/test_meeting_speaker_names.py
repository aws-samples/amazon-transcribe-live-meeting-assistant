#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The meeting assistant names speakers as the meeting page does.

Its transcript tool and its key-frames tool read the segments straight from
DynamoDB, so they apply the meeting's speaker identity map and speaker modes
themselves (lma_speakers): one person's voices read as one name, a room's voices
stay apart, and a voice someone named is shown under that name.
"""

from __future__ import annotations

import json
import os
import sys
import types
from typing import Any, Dict, List

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import meeting_frames  # noqa: E402
import strands_meeting_assist_function as handler_module  # noqa: E402

SEGMENTS: List[Dict[str, Any]] = [
    {"StartTime": 0, "EndTime": 100, "Speaker": "Bob Strahan (spk_0)", "Channel": "REMOTE", "Transcript": "Shall we start?"},
    {"StartTime": 100, "EndTime": 101, "Speaker": "Bob Strahan (spk_1)", "Channel": "REMOTE", "Transcript": "Yes."},
    {"StartTime": 101, "EndTime": 110, "Speaker": "ConfRm_1 (spk_0)", "Channel": "REMOTE", "Transcript": "Hello."},
    {"StartTime": 110, "EndTime": 120, "Speaker": "ConfRm_1 (spk_1)", "Channel": "REMOTE", "Transcript": "Hi."},
    {"StartTime": 121, "EndTime": 122, "Speaker": "", "Channel": "AGENT_ASSISTANT", "Transcript": "Noted."},
]
IDENTITIES = json.dumps({"identities": {"REMOTE#ConfRm_1 (spk_1)": {"name": "Priya"}}})


class FakeDynamo:
    """The boto3 DynamoDB resource: one meeting record, and the keys asked for."""

    def __init__(self, record=None, error=None):
        self.record = record
        self.error = error
        self.asked: List[Dict[str, Any]] = []

    def Table(self, _name):  # noqa: N802 - boto3's name
        fake = self

        class _Table:
            @staticmethod
            def get_item(**kwargs):
                fake.asked.append(kwargs)
                if fake.error:
                    raise fake.error
                return {"Item": fake.record} if fake.record else {}

        return _Table()


@pytest.fixture
def meeting(monkeypatch):
    def install(record=None, error=None):
        fake = FakeDynamo(record, error)
        monkeypatch.setattr(handler_module, "dynamodb", fake)
        monkeypatch.setattr(handler_module, "query_transcript_items", lambda *_a, **_kw: [dict(s) for s in SEGMENTS])
        return fake

    return install


def test_the_transcript_names_speakers_as_the_meeting_page_does(meeting):
    fake = meeting({"SpeakerIdentities": IDENTITIES})
    text = handler_module.fetch_meeting_transcript("m-1", "calls")
    assert text.split("\n") == [
        "Bob Strahan: Shall we start?",
        "Bob Strahan: Yes.",
        "ConfRm_1 · Voice 1: Hello.",
        "Priya: Hi.",
        "MeetingAssistant: Noted.",
    ]
    assert fake.asked[0]["Key"] == {"PK": "c#m-1", "SK": "c#m-1"}


def test_a_chosen_speaker_mode_applies(meeting):
    meeting({"SpeakerIdentities": json.dumps({"speakerModes": {"REMOTE#Bob Strahan": {"mode": "several"}}})})
    lines = handler_module.fetch_meeting_transcript("m-1", "calls").split("\n")
    assert lines[:2] == ["Bob Strahan · Voice 1: Shall we start?", "Bob Strahan · Voice 2: Yes."]


def test_an_unreadable_meeting_record_still_applies_the_default_modes(meeting):
    meeting(error=RuntimeError("throttled"))
    lines = handler_module.fetch_meeting_transcript("m-1", "calls").split("\n")
    assert lines[:4] == [
        "Bob Strahan: Shall we start?",
        "Bob Strahan: Yes.",
        "ConfRm_1 · Voice 1: Hello.",
        "ConfRm_1 · Voice 2: Hi.",
    ]


def test_without_the_configuration_layer_speakers_are_as_stored(meeting, monkeypatch):
    meeting({"SpeakerIdentities": IDENTITIES})
    monkeypatch.setattr(handler_module, "speaker_namer", None)
    lines = handler_module.fetch_meeting_transcript("m-1", "calls").split("\n")
    assert lines[1] == "Bob Strahan (spk_1): Yes."


def test_the_key_frames_tool_names_speakers_the_same_way(meeting, monkeypatch):
    meeting({"SpeakerIdentities": IDENTITIES})
    strands = types.ModuleType("strands")
    strands.tool = lambda fn: fn
    monkeypatch.setitem(sys.modules, "strands", strands)
    monkeypatch.setattr(handler_module, "APPSYNC_GRAPHQL_URL", "")
    monkeypatch.setattr(
        handler_module.meeting_frames,
        "load_keyframes",
        lambda _run, _cid: [{"FrameId": "f1", "Timestamp": 50, "SlideText": "Agenda"}],
    )
    monkeypatch.setattr(handler_module.call_metering, "record", lambda *a, **kw: True)
    get_meeting_frames = handler_module.create_meeting_frames_tool(
        call_id="m-1", dynamodb_table_name="calls", model_id="amazon.nova-micro-v1:0", show_inline=False
    )
    text = "\n".join(block.get("text", "") for block in get_meeting_frames(last=1)["content"])
    assert "Bob Strahan: Yes." in text
    assert "Priya: Hi." in text
    assert "(spk_" not in text


def test_frames_without_a_namer_use_the_stored_speaker():
    said = meeting_frames.transcript_between(SEGMENTS[:2], 0, None)
    assert said == "[00:00] Bob Strahan (spk_0): Shall we start?\n[01:40] Bob Strahan (spk_1): Yes."
