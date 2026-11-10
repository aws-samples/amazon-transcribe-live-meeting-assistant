# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Who each transcript line is shown as: the default speaker mode and the identity map."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import lma_speakers as speakers  # noqa: E402

REPO = HERE.parents[4]
FIXTURE = REPO / "test-fixtures" / "speaker-modes.json"
UI_RULES = (
    REPO
    / "lma-ai-stack"
    / "source"
    / "ui"
    / "src"
    / "components"
    / "common"
    / "speaker-mode-rules.json"
)
TABLE = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES = TABLE["cases"]


def _rows(case):
    """The case's segments under the stored attribute names, as DynamoDB holds them."""
    rows = []
    for index, segment in enumerate(case["segments"]):
        rows.append(
            {
                "Channel": segment["channel"],
                "Speaker": segment["speaker"],
                "StartTime": segment["start"],
                "EndTime": segment["end"],
                "IsPartial": segment.get("isPartial", False),
                "SegmentId": segment.get("segmentId", f"seg-{index}"),
            }
        )
    return rows


def _raw(case):
    identities = case.get("identities")
    return json.dumps(identities) if isinstance(identities, dict) else identities


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_names_match_the_shared_table(case) -> None:
    rows = _rows(case)
    name = speakers.speaker_namer(rows, _raw(case), case.get("fallbackName"))
    assert [name(row) for row in rows] == case["names"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_groups_match_the_shared_table(case) -> None:
    rows = _rows(case)
    groups = speakers.speaker_groups(rows, speakers.parse_speaker_identities(_raw(case)))
    got = {
        key: {
            "mode": group["mode"],
            "default": group["default"],
            "reason": group["reason"],
            "voices": [voice["id"] for voice in group["voices"]],
        }
        for key, group in groups.items()
    }
    assert got == case["groups"]


@pytest.mark.parametrize("name", TABLE["roomNames"]["rooms"])
def test_room_names_read_as_rooms(name) -> None:
    assert speakers.looks_like_room(name)


@pytest.mark.parametrize("name", TABLE["roomNames"]["people"])
def test_people_do_not_read_as_rooms(name) -> None:
    assert not speakers.looks_like_room(name)


@pytest.mark.parametrize("name", TABLE["roomNames"]["unknown"])
def test_names_that_stand_for_nobody_are_unknown(name) -> None:
    assert speakers.is_unknown_name(name)


def test_the_ui_copy_of_the_rules_is_identical() -> None:
    layer_copy = (HERE / "speaker_mode_rules.json").read_bytes()
    assert UI_RULES.read_bytes() == layer_copy, (
        "copy lma_speakers/speaker_mode_rules.json to "
        "ui/src/components/common/speaker-mode-rules.json"
    )


def test_every_room_pattern_is_exemplified() -> None:
    """Each pattern earns its place: at least one listed room name matches it alone."""
    rooms = TABLE["roomNames"]["rooms"]
    for pattern in speakers.ROOM_NAME_PATTERNS:
        assert any(pattern.search(name) for name in rooms), pattern.pattern


def test_a_segment_with_no_speaker_stays_unattributed() -> None:
    """The one documented difference from the UI, which must show something."""
    row = {"Channel": "REMOTE", "Speaker": "", "StartTime": 0, "EndTime": 1}
    assert speakers.speaker_namer([row], None, "Sam")(row) == ""


def test_times_stored_as_decimals_are_read() -> None:
    from decimal import Decimal

    rows = [
        {
            "Channel": "REMOTE",
            "Speaker": "Jo (spk_0)",
            "StartTime": Decimal("0"),
            "EndTime": Decimal("90"),
        },
        {
            "Channel": "REMOTE",
            "Speaker": "Jo (spk_1)",
            "StartTime": Decimal("90"),
            "EndTime": Decimal("200"),
        },
    ]
    groups = speakers.speaker_groups(rows, speakers.parse_speaker_identities(None))
    assert groups["REMOTE#Jo"]["mode"] == speakers.SEVERAL_PEOPLE


def test_other_channels_form_no_group() -> None:
    rows = [
        {"Channel": "MEETING_CHAT", "Speaker": "Bob (spk_0)", "StartTime": 0, "EndTime": 1},
        {"Channel": "AGENT_ASSISTANT", "Speaker": "Bob (spk_1)", "StartTime": 0, "EndTime": 1},
    ]
    assert speakers.speaker_groups(rows, speakers.parse_speaker_identities(None)) == {}


def test_the_map_keeps_its_sections_whatever_it_holds() -> None:
    parsed = speakers.parse_speaker_identities('{"identities": [], "speakerModes": 3}')
    assert parsed == {"identities": {}, "merges": {}, "segmentOverrides": {}, "speakerModes": {}}
