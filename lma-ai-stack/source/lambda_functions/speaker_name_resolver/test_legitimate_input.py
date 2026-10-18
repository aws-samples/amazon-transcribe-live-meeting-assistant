# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Every guard must pass legitimate input.

Four review rounds of this feature each found a defect in the previous round's
*fix* rather than in the naming logic. The common thread: each guard was written
to reject something bad, and then also rejected something good --

  * the placeholder-name regex rejected any long digit run by taking 96 seconds
    over it;
  * the cost bound rejected every pass on a meeting over 160 turns;
  * the growth gate rejected every press of the button on an ended meeting;
  * the whole-word match rejected every name in Chinese, Japanese or Thai;
  * the introduction check rejected every introduction not in English;
  * the apostrophe split rejected every O'Brien.

Each of those was caught by a human reading the code, because the suite only
ever asserted that guards BLOCK what they should. This file asserts the other
half: that a meeting a real person would recognise as nameable actually gets
named, end to end through the real `pure` functions, across the shapes that have
bitten so far. A guard that over-rejects fails here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

TABLE = "lma-call-events"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", TABLE)
os.environ.setdefault("APPSYNC_GRAPHQL_URL", "https://example/graphql")
os.environ.setdefault("BEDROCK_MODEL_ID", "test.model")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

spec = importlib.util.spec_from_file_location("legit_index", HERE / "index.py")
index = importlib.util.module_from_spec(spec)
sys.modules["legit_index"] = index
spec.loader.exec_module(index)
pure = index.pure

CALL_ID = "meeting-1"
OWNER = "owner@example.com"


def segment(speaker, start, text, channel="CALLER", segment_id=None):
    return {
        "PK": {"S": f"trs#{CALL_ID}"},
        "SK": {"S": f"{start}#{channel}"},
        "SegmentId": {"S": segment_id or f"s{start}"},
        "Channel": {"S": channel},
        "Speaker": {"S": speaker},
        "Transcript": {"S": text},
        "StartTime": {"N": str(start)},
        "EndTime": {"N": str(start + 4)},
        "IsPartial": {"BOOL": False},
    }


def meeting_row(**extra):
    row = {
        "PK": {"S": f"c#{CALL_ID}"},
        "SK": {"S": f"c#{CALL_ID}"},
        "CallId": {"S": CALL_ID},
        "Owner": {"S": OWNER},
    }
    row.update(extra)
    return row


class Dynamo:
    def __init__(self, meeting, segments):
        self.meeting = meeting
        self.segments = segments

    def get_item(self, **kwargs):
        return {"Item": self.meeting}

    def query(self, **kwargs):
        return {"Items": list(self.segments)}


class HonestModel:
    """A model that reports what the transcript actually says.

    Deliberately not a canned answer: it finds the name it was told to look for
    in the turn that contains it and cites that turn honestly, which is what a
    well-behaved model does. So a failure here is the code refusing a correct
    answer, never the model misbehaving.
    """

    def __init__(self, name, basis="self-introduction"):
        self.name = name
        self.basis = basis
        self.prompts = []

    def converse(self, **kwargs):
        prompt = kwargs["messages"][0]["content"][0]["text"]
        self.prompts.append(prompt)
        proposals = []
        for line in prompt.splitlines():
            if not line.startswith("[") or self.name not in line:
                continue
            number = int(line[1 : line.index("]")])
            label = line[line.index("] ") + 2 : line.index(": ")]
            if self.basis == pure.ADDRESSED_BY_NAME:
                # The name belongs to whoever spoke the previous turn.
                number_label = self._previous_label(prompt, number)
                if number_label is None:
                    continue
                label = number_label
            proposals.append(
                {"label": label, "name": self.name, "basis": self.basis, "turn": number}
            )
            break
        return {"output": {"message": {"content": [{"text": json.dumps(proposals)}]}}}

    @staticmethod
    def _previous_label(prompt, number):
        for line in prompt.splitlines():
            if line.startswith(f"[{number - 1}] "):
                return line[line.index("] ") + 2 : line.index(": ")]
        return None


def run(monkeypatch, segments, name, *, meeting=None, basis="self-introduction", **event_extra):
    ddb = Dynamo(meeting or meeting_row(), segments)
    model = HonestModel(name, basis=basis)
    written = []
    monkeypatch.setattr(index, "dynamodb", ddb)
    monkeypatch.setattr(index, "bedrock", model)
    monkeypatch.setattr(index, "write_identities", lambda c, i: written.append(i))
    result = index.handler({"CallId": CALL_ID, **event_extra}, None)
    return result, written, model


SPK0 = "CALLER#Other Participant (spk_0)"


# --------------------------------------------------------------------------
# A name in any script, phrased any way, must reach the transcript
# --------------------------------------------------------------------------

# A bare name with no introducing phrase at all -- "Sarah Lee, platform
# engineering" -- is beyond what a cheap regex can spot, so the cost shortcut
# skips it. It is covered separately below, because the end-of-meeting pass and
# the button both bypass that shortcut and do find it.
INTRODUCTIONS = [
    # (language note, turn text, the name in it)
    ("English, canonical", "Hi everyone, I'm Sarah Lee and I run platform", "Sarah Lee"),
    ("English, terse", "Sarah Lee here", "Sarah Lee"),
    ("English, apostrophe surname", "Hi, I'm Siobhan O'Brien", "Siobhan O'Brien"),
    ("English, hyphenated", "I'm Jean-Luc Picard", "Jean-Luc Picard"),
    ("English, single name", "I'm Priya", "Priya"),
    ("English, two-letter name", "Hi, I'm An", "An"),
    ("French", "Je m'appelle Sarah Lee", "Sarah Lee"),
    ("German", "Ich bin Sarah Lee", "Sarah Lee"),
    ("Spanish", "Me llamo Sarah Lee", "Sarah Lee"),
    ("Russian", "Меня зовут Иван Петров", "Иван Петров"),
    ("Greek", "Με λένε Ανδρέας", "Ανδρέας"),
    ("Arabic", "اسمي أحمد", "أحمد"),
    ("Hindi", "मेरा नाम नरेन्द्र है", "नरेन्द्र"),
    ("Chinese", "大家好，我是李明", "李明"),
    ("Japanese", "こんにちは、田中です", "田中"),
    ("Thai", "สวัสดีครับผมสมชาย", "สมชาย"),
]


@pytest.mark.parametrize(("note", "text", "name"), INTRODUCTIONS, ids=[i[0] for i in INTRODUCTIONS])
def test_a_real_introduction_names_the_speaker(
    monkeypatch: pytest.MonkeyPatch, note: str, text: str, name: str
) -> None:
    """Whatever the language or phrasing, the name has to reach the transcript.

    It may arrive confirmed or as a suggestion -- that distinction is about how
    much to trust it. What must never happen is nothing at all.
    """
    segments = [segment("Other Participant (spk_0)", 0, text)]
    result, written, model = run(monkeypatch, segments, name)
    assert model.prompts, f"{note}: the pass did not even reach the model"
    assert result["NamesApplied"] == 1, f"{note}: {name!r} was not applied from {text!r}"
    assert written[-1]["identities"][SPK0]["name"] == name, note


@pytest.mark.parametrize(("note", "text", "name"), INTRODUCTIONS, ids=[i[0] for i in INTRODUCTIONS])
def test_the_name_reaches_the_llm_facing_transcript(
    monkeypatch: pytest.MonkeyPatch, note: str, text: str, name: str
) -> None:
    """Naming is pointless if the summary still reads a voice id."""
    segments = [segment("Other Participant (spk_0)", 0, text)]
    _, written, _ = run(monkeypatch, segments, name)
    stored = written[-1]
    resolved = pure.parse_identities(json.dumps(stored))
    row = {"Channel": "CALLER", "Speaker": "Other Participant (spk_0)", "SegmentId": "s0"}
    target = pure.resolve_merge_target(
        pure.speaker_key(row["Channel"], row["Speaker"]), resolved["merges"]
    )
    assert resolved["identities"][target]["name"] == name, note


# --------------------------------------------------------------------------
# Meeting shapes that have silenced naming before
# --------------------------------------------------------------------------


def long_meeting(introduction_at, name, total=400):
    rows = []
    for i in range(total):
        text = f"so the number for week {i} was about three percent"
        if i == introduction_at:
            text = f"Hi everyone, I'm {name}"
        rows.append(segment("Other Participant (spk_0)", i * 5, text, segment_id=f"s{i}"))
    return rows


# The trim window on a 400-turn meeting is turns 1-106 and 347-400, so 0 and 50
# are inside it, 200 is outside, and 399 is inside the tail.
@pytest.mark.parametrize("introduction_at", [0, 50, 200, 399])
def test_a_long_meeting_is_still_named_wherever_the_introduction_falls(
    monkeypatch: pytest.MonkeyPatch, introduction_at: int
) -> None:
    """A 400-turn meeting is ordinary. An introduction at the start, the middle
    or the very end must all be findable -- the middle one only if the trim
    window reaches it, which is why this is parametrised rather than asserted
    once."""
    rows = long_meeting(introduction_at, "Sarah Lee")
    result, written, model = run(monkeypatch, rows, "Sarah Lee")
    if introduction_at in (0, 50, 399):
        assert result["NamesApplied"] == 1, f"introduction at turn {introduction_at} was missed"
        assert written[-1]["identities"][SPK0]["name"] == "Sarah Lee"
    else:
        # Outside the trim window is a documented limitation. Nothing in the
        # window names anybody either, so the shortcut legitimately declines --
        # what must not happen is a name being applied to the wrong voice.
        assert result["NamesApplied"] == 0


def test_a_long_meeting_can_be_named_on_a_later_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate that compares transcript size once silenced every pass after the
    first on any meeting past the trim limit."""
    stamped = meeting_row(
        SpeakerIdentities={
            "S": json.dumps(
                {"identities": {}, "lastPass": {"at": "2026-01-01T00:00:00+00:00", "turns": 160}}
            )
        }
    )
    rows = long_meeting(0, "Sarah Lee")
    result, _, model = run(monkeypatch, rows, "Sarah Lee", meeting=stamped)
    assert model.prompts, "a 400-turn meeting was declined after an earlier pass"
    assert result["NamesApplied"] == 1


def test_the_button_works_on_an_ended_meeting_that_has_had_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transcript of an ended meeting can never grow, so a gate keyed on
    growth refused the button for ever."""
    stamped = meeting_row(
        SpeakerIdentities={
            "S": json.dumps(
                {"identities": {}, "lastPass": {"at": "2026-01-01T00:00:00+00:00", "turns": 1}}
            )
        }
    )
    segments = [segment("Other Participant (spk_0)", 0, "Hi, I'm Sarah Lee")]
    ddb = Dynamo(stamped, segments)
    model = HonestModel("Sarah Lee")
    written = []
    monkeypatch.setattr(index, "dynamodb", ddb)
    monkeypatch.setattr(index, "bedrock", model)
    monkeypatch.setattr(index, "write_identities", lambda c, i: written.append(i))
    result = index.handler(
        {
            "info": {"fieldName": "resolveSpeakerNames"},
            "arguments": {"input": {"CallId": CALL_ID, "Force": True}},
            "identity": {"username": OWNER},
        },
        None,
    )
    assert model.prompts, "the button did not reach the model"
    assert result["NamesApplied"] == 1


def test_a_pasted_attendee_list_is_actually_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented recovery when automatic naming gets a spelling wrong: paste
    the names and ask again. It has to reach the model with them."""
    stamped = meeting_row(
        SpeakerIdentities={
            "S": json.dumps(
                {
                    "identities": {},
                    "candidates": ["Sarah Lee", "Priya Raman"],
                    "lastPass": {"at": "2026-01-01T00:00:00+00:00", "turns": 1},
                }
            )
        }
    )
    segments = [segment("Other Participant (spk_0)", 0, "Hi, I'm Sarah Lee")]
    ddb = Dynamo(stamped, segments)
    model = HonestModel("Sarah Lee")
    monkeypatch.setattr(index, "dynamodb", ddb)
    monkeypatch.setattr(index, "bedrock", model)
    monkeypatch.setattr(index, "write_identities", lambda c, i: None)
    index.handler(
        {
            "info": {"fieldName": "resolveSpeakerNames"},
            "arguments": {"input": {"CallId": CALL_ID, "Force": True}},
            "identity": {"username": OWNER},
        },
        None,
    )
    assert model.prompts, "the button was declined, so the attendee list was never used"
    assert "Sarah Lee" in model.prompts[0]
    assert "Priya Raman" in model.prompts[0]


def test_the_end_of_meeting_pass_is_never_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is the pass the summary depends on, so no gate may refuse it."""
    just_stamped = meeting_row(
        SpeakerIdentities={
            "S": json.dumps(
                {
                    "identities": {},
                    "lastPass": {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "turns": 400,
                    },
                }
            )
        }
    )
    rows = long_meeting(0, "Sarah Lee")
    result, _, model = run(
        monkeypatch, rows, "Sarah Lee", meeting=just_stamped, Trigger="meeting-end"
    )
    assert model.prompts, "the end-of-meeting pass was declined"
    assert result["NamesApplied"] == 1


def test_being_addressed_by_name_still_produces_a_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The weaker of the two signals must still work, as a suggestion."""
    segments = [
        segment("Other Participant (spk_0)", 0, "so the number was three percent", segment_id="a"),
        segment("Other Participant (spk_1)", 10, "That is a good point, Sarah", segment_id="b"),
    ]
    result, written, model = run(monkeypatch, segments, "Sarah", basis=pure.ADDRESSED_BY_NAME)
    assert model.prompts
    assert result["NamesApplied"] == 1
    assert written[-1]["identities"][SPK0]["name"] == "Sarah"
    assert written[-1]["identities"][SPK0]["status"] == "proposed"


def test_a_two_speaker_conference_room_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case the whole feature exists for."""
    segments = [
        segment("Other Participant (spk_0)", 0, "Hi everyone, I'm Sarah Lee", segment_id="a"),
        segment("Other Participant (spk_1)", 10, "And I'm Priya Raman", segment_id="b"),
    ]
    _, written, _ = run(monkeypatch, segments, "Sarah Lee")
    assert written[-1]["identities"][SPK0]["name"] == "Sarah Lee"


# --------------------------------------------------------------------------
# And the guards still block what they should
# --------------------------------------------------------------------------


def test_a_transcript_naming_nobody_names_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [
        segment("Other Participant (spk_0)", 0, "the number for week one was three percent")
    ]
    result, written, _ = run(monkeypatch, segments, "Sarah Lee")
    assert result["NamesApplied"] == 0
    # A declined pass writes nothing at all, so there may be no write to inspect.
    assert not written or written[-1]["identities"] == {}


def test_a_third_party_mention_names_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    """'Bob said yesterday that...' is not Bob speaking."""
    segments = [
        segment("Other Participant (spk_0)", 0, "Bob said yesterday that the budget is fine")
    ]
    # The honest model cites the turn containing the name as a self-introduction,
    # which is wrong -- and the turn does not read like one, so it must not be
    # applied as settled.
    _, written, _ = run(monkeypatch, segments, "Bob")
    entry = written[-1]["identities"].get(SPK0) if written else None
    assert entry is None or entry["status"] == "proposed"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_a_bare_name_introduction_is_found_at_the_end_of_the_meeting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Sarah Lee, platform engineering" has no phrase a cheap regex can spot, so
    the cost shortcut skips it during the meeting. The end-of-meeting pass
    bypasses that shortcut, so the name is still found -- just later."""
    segments = [segment("Other Participant (spk_0)", 0, "Sarah Lee, platform engineering")]
    result, written, model = run(monkeypatch, segments, "Sarah Lee", Trigger="meeting-end")
    assert model.prompts, "the end-of-meeting pass must not be shortcut"
    assert result["NamesApplied"] == 1
    assert written[-1]["identities"][SPK0]["name"] == "Sarah Lee"


def test_a_bare_name_introduction_is_found_when_the_button_is_pressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segments = [segment("Other Participant (spk_0)", 0, "Sarah Lee, platform engineering")]
    ddb = Dynamo(meeting_row(), segments)
    model = HonestModel("Sarah Lee")
    written = []
    monkeypatch.setattr(index, "dynamodb", ddb)
    monkeypatch.setattr(index, "bedrock", model)
    monkeypatch.setattr(index, "write_identities", lambda c, i: written.append(i))
    result = index.handler(
        {
            "info": {"fieldName": "resolveSpeakerNames"},
            "arguments": {"input": {"CallId": CALL_ID, "Force": True}},
            "identity": {"username": OWNER},
        },
        None,
    )
    assert model.prompts
    assert result["NamesApplied"] == 1
