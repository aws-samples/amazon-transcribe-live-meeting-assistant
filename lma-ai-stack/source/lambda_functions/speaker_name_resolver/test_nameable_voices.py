# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Which voices still need a name, by the table the Speakers panel is tested against.

The panel runs automatic naming passes while it counts voices waiting for a
name, and this module answers ALREADY_NAMED when it finds none. Each side
deciding that on its own let them disagree -- the panel kept asking, once a
minute, for passes this module would never run -- so both are held to
test-fixtures/nameable-voices.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
sys.path.insert(0, str(HERE))

import pure  # noqa: E402

FIXTURE = HERE.parents[3] / "test-fixtures" / "nameable-voices.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _segments(case):
    return [
        {
            "Channel": s["channel"],
            "Speaker": s["speaker"],
            "Transcript": s["transcript"],
            "StartTime": s["start"],
            "EndTime": s["start"] + 1,
            "IsPartial": s.get("isPartial", False),
            "SegmentId": f"seg-{index}",
        }
        for index, s in enumerate(case["segments"])
    ]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_unresolved_voices_match_the_shared_table(case) -> None:
    identities = pure.parse_identities(json.dumps(case.get("identities") or {}))
    turns = pure.build_turns(_segments(case))
    capture_source = case.get("captureSource")
    assert sorted(pure.unresolved_voices(turns, identities, turns, capture_source)) == case["keys"]


def test_a_virtual_participant_meeting_has_no_channel_labels() -> None:
    """One roster name with two voices is a channel label only without a VP record."""
    turns = pure.build_turns(
        _segments(
            {
                "segments": [
                    {"channel": "REMOTE", "speaker": "Bob (spk_0)", "start": 0, "transcript": "a"},
                    {"channel": "REMOTE", "speaker": "Bob (spk_1)", "start": 5, "transcript": "b"},
                ]
            }
        )
    )
    assert pure.channel_label_bases(turns) == {("REMOTE", "Bob")}
    assert pure.channel_label_bases(turns, "STREAM_AUDIO") == {("REMOTE", "Bob")}
    assert pure.channel_label_bases(turns, "VIRTUAL_PARTICIPANT") == set()
    assert pure.channel_label_bases(turns, " virtual_participant ") == set()


def test_a_virtual_participant_attendee_split_in_two_costs_no_model_call() -> None:
    turns = pure.build_turns(
        _segments(
            {
                "segments": [
                    {
                        "channel": "REMOTE",
                        "speaker": "Bob (spk_0)",
                        "start": 0,
                        "transcript": "Hi, I'm Bob",
                    },
                    {"channel": "REMOTE", "speaker": "Bob (spk_1)", "start": 5, "transcript": "ok"},
                ]
            }
        )
    )
    identities = pure.parse_identities("{}")
    assert pure.worth_a_model_call(turns, identities, force=True, all_turns=turns)
    assert not pure.worth_a_model_call(
        turns, identities, force=True, all_turns=turns, capture_source="VIRTUAL_PARTICIPANT"
    )


def test_a_pass_keeps_the_sections_it_does_not_work_on() -> None:
    """A pass writes the whole map back; the speaker modes must survive it."""
    stored = {
        "identities": {},
        "speakerModes": {"CALLER#ConfRm_1": {"mode": "several"}},
        "fromANewerVersion": {"x": 1},
    }
    parsed = pure.parse_identities(json.dumps(stored))
    assert parsed["speakerModes"] == {"REMOTE#ConfRm_1": {"mode": "several"}}
    assert parsed["fromANewerVersion"] == {"x": 1}
    merged, _ = pure.merge_applied_onto(parsed, {})
    assert merged["speakerModes"] == {"REMOTE#ConfRm_1": {"mode": "several"}}
    assert merged["fromANewerVersion"] == {"x": 1}
    assert pure.stamp_pass(merged, "2026-11-06T00:00:00+00:00", 3)["speakerModes"]
