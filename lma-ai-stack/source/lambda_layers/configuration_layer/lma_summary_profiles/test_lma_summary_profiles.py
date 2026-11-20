# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Summary profile names, the profile listing, and structured action items."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import lma_summary_profiles as sp  # noqa: E402

SECTION = {
    "discovery-session": {
        "label": "Discovery session",
        "description": "Workshops",
        "structuredActionItems": True,
        "prompts": {"1#SUMMARY": "{transcript}"},
    },
    "healthcare": {"label": "", "prompts": {"1#SOAP": "{transcript}"}},
    # Never a profile of the section: default is the Transcript Summary prompts.
    "default": {"label": "Not this one", "prompts": {}},
    "Bad Name": {"label": "x"},
    "not-an-object": "text",
}

SEGMENTS = [
    {"segmentId": "s1", "startTime": 0.0, "endTime": 10.0},
    {"segmentId": "s2", "startTime": 10.0, "endTime": 25.0},
    {"segmentId": "s3", "startTime": 30.0, "endTime": 40.0},
]
NAMES = ["Priya Shah", "Sam Lee"]
TRANSCRIPT = "Priya Shah: I will send the plan. Sam Lee: Jordan will review it."


# ── names ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "  ", "default", " default "])
def test_no_profile_and_default_are_stored_as_none(value) -> None:
    assert sp.normalize_profile_name(value) is None


@pytest.mark.parametrize("value", ["Discovery", "a" * 64, "-x", "a_b", 3, ["x"]])
def test_a_malformed_profile_name_is_refused(value) -> None:
    with pytest.raises(ValueError):
        sp.normalize_profile_name(value)


def test_the_entries_leave_out_default_and_malformed_ones() -> None:
    assert set(sp.profile_entries(SECTION)) == {"discovery-session", "healthcare"}
    assert sp.profile_entries(None) == {}


def test_the_listing_starts_with_default_and_labels_each_profile() -> None:
    listing = sp.profile_listing(SECTION)
    assert [entry["name"] for entry in listing] == ["default", "discovery-session", "healthcare"]
    assert listing[1]["label"] == "Discovery session"
    assert listing[1]["structuredActionItems"] is True
    # A profile without a label is listed under its name.
    assert listing[2]["label"] == "healthcare"


def test_a_profile_name_is_checked_against_the_configuration() -> None:
    assert sp.require_profile("discovery-session", SECTION) == "discovery-session"
    assert sp.require_profile("default", SECTION) is None
    with pytest.raises(sp.UnknownSummaryProfile) as raised:
        sp.require_profile("stand-up", SECTION)
    assert "default, discovery-session, healthcare" in str(raised.value)


# ── action items ────────────────────────────────────────────────────────────


def _answer(items) -> str:
    return json.dumps(items)


def test_valid_action_items_are_kept_with_the_segment_at_their_time() -> None:
    items = sp.validate_action_items(
        _answer(
            [
                {
                    "text": "Send the plan",
                    "owner": "Priya Shah",
                    "dueDate": "2026-12-01",
                    "dueText": "by the first",
                    "startTime": 12.5,
                }
            ]
        ),
        SEGMENTS,
        NAMES,
        TRANSCRIPT,
    )
    assert items == [
        {
            "text": "Send the plan",
            "owner": "Priya Shah",
            "dueDate": "2026-12-01",
            "dueText": "by the first",
            "startTime": 12.5,
            "segmentId": "s2",
        }
    ]


def test_an_owner_named_in_the_transcript_but_not_speaking_is_kept() -> None:
    items = sp.validate_action_items(
        _answer([{"text": "Review it", "owner": "Jordan", "startTime": 27}]),
        SEGMENTS,
        NAMES,
        TRANSCRIPT,
    )
    assert items[0]["owner"] == "Jordan"
    # Between two segments: the one that started last before it.
    assert items[0]["segmentId"] == "s2"


def test_an_owner_nobody_named_is_dropped_rather_than_kept() -> None:
    items = sp.validate_action_items(
        _answer([{"text": "Book the room", "owner": "The project manager", "startTime": 1}]),
        SEGMENTS,
        NAMES,
        TRANSCRIPT,
    )
    assert items[0]["owner"] is None


def test_a_due_date_that_is_not_a_real_date_is_dropped_and_its_wording_kept() -> None:
    items = sp.validate_action_items(
        _answer(
            [
                {"text": "a", "dueDate": "2026-02-30", "dueText": "end of February"},
                {"text": "b", "dueDate": "next Friday"},
            ]
        ),
        SEGMENTS,
    )
    assert [(i["dueDate"], i["dueText"]) for i in items] == [
        (None, "end of February"),
        (None, None),
    ]


def test_a_time_outside_the_meeting_is_dropped_with_its_segment() -> None:
    items = sp.validate_action_items(
        _answer([{"text": "a", "startTime": 400}, {"text": "b", "startTime": -1}]), SEGMENTS
    )
    assert all(i["startTime"] is None and i["segmentId"] is None for i in items)


def test_a_segment_id_the_model_wrote_is_never_used() -> None:
    items = sp.validate_action_items(
        _answer([{"text": "a", "startTime": 35, "segmentId": "invented"}]), SEGMENTS
    )
    assert items[0]["segmentId"] == "s3"


def test_elements_without_text_or_not_objects_are_dropped() -> None:
    items = sp.validate_action_items(
        _answer([{"text": ""}, "a string", 3, {"owner": "Priya Shah"}, {"text": "  keep  "}]),
        SEGMENTS,
    )
    assert [i["text"] for i in items] == ["keep"]


def test_wrong_types_become_null() -> None:
    items = sp.validate_action_items(
        _answer([{"text": "a", "owner": 5, "dueDate": 20261201, "dueText": [], "startTime": True}]),
        SEGMENTS,
    )
    assert items[0] == {
        "text": "a",
        "owner": None,
        "dueDate": None,
        "dueText": None,
        "startTime": None,
        "segmentId": None,
    }


def test_a_fenced_or_introduced_answer_is_read() -> None:
    fenced = 'Here they are:\n```json\n[{"text": "a"}]\n```'
    assert sp.validate_action_items(fenced)[0]["text"] == "a"
    introduced = 'The items: [{"text": "b"}] and nothing else.'
    assert sp.validate_action_items(introduced)[0]["text"] == "b"


@pytest.mark.parametrize(
    "answer",
    ["", None, "There were no action items.", '{"text": "an object, not a list"}', "[not json"],
)
def test_a_malformed_answer_is_refused(answer) -> None:
    with pytest.raises(sp.MalformedActionItems):
        sp.validate_action_items(answer, SEGMENTS)


def test_an_empty_list_is_no_action_items() -> None:
    assert sp.validate_action_items("[]", SEGMENTS) == []


def test_at_most_fifty_items_are_kept() -> None:
    items = sp.validate_action_items(_answer([{"text": str(n)} for n in range(80)]))
    assert len(items) == sp.MAX_ACTION_ITEMS


def test_the_stored_form_round_trips_to_the_output_form() -> None:
    items = sp.validate_action_items(
        _answer([{"text": "a", "owner": "Sam Lee", "startTime": 5}]), SEGMENTS, NAMES
    )
    stored = sp.stored_action_items(items)
    assert stored[0]["Text"] == "a" and stored[0]["SegmentId"] == "s1"
    assert sp.action_items_output(stored) == items
