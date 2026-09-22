# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the Contact Lens transforms in eventprocessor_utils.

Three functions turn a Contact Lens payload into the shapes the AppSync mutations
take, and all three do arithmetic and string slicing that produces a plausible
answer whatever it gets wrong:

* `transform_contact_lens_segment` — a transcript row, from either a partial
  (`Utterance`) or a final (`Transcript`) segment. Partials are **accumulated in a
  module-level dict**, `UTTERANCES_MAP`, and removed when the final for that id
  arrives; get that wrong and a meeting either loses text or repeats it.
* `transform_segment_to_categories_agent_assist` — a category match, timed by the
  span across every point of interest.
* `transform_segment_to_issues_agent_assist` — an issue, whose text is cut out of
  the surrounding transcript by character offsets. An off-by-one here shows up as
  a truncated sentence on the meeting page, not as an error.

Contact Lens reports times in milliseconds and the rest of LMA works in seconds,
so every one of these divides by 1000 — the conversion is asserted explicitly
because a missed division is three orders of magnitude wrong and still renders.

No AWS: all three are pure functions over a dict.
"""

from __future__ import annotations

import pytest

import eventprocessor_utils.eventprocessor as ep
from eventprocessor_utils.eventprocessor import (
    transform_contact_lens_segment,
    transform_segment_to_categories_agent_assist,
    transform_segment_to_issues_agent_assist,
)


@pytest.fixture(autouse=True)
def _clear_utterances_map():
    """`UTTERANCES_MAP` is module state, so one test's partials would leak."""
    ep.UTTERANCES_MAP.clear()
    yield
    ep.UTTERANCES_MAP.clear()


def partial_segment(transcript_id: str = "t1", content: str = "hello", **overrides) -> dict:
    segment = {
        "CallId": "contact-1",
        "Utterance": {
            "TranscriptId": transcript_id,
            "PartialContent": content,
            "ParticipantRole": "AGENT",
            "BeginOffsetMillis": 1000,
            "EndOffsetMillis": 2000,
        },
    }
    segment["Utterance"].update(overrides)
    return segment


def final_segment(segment_id: str = "t1", content: str = "hello there", **overrides) -> dict:
    segment = {
        "CallId": "contact-1",
        "Transcript": {
            "Id": segment_id,
            "Content": content,
            "ParticipantRole": "AGENT",
            "BeginOffsetMillis": 1000,
            "EndOffsetMillis": 3000,
        },
    }
    segment["Transcript"].update(overrides)
    return segment


# ── partial and final transcripts ───────────────────────────────────────────


def test_an_utterance_is_marked_partial():
    assert transform_contact_lens_segment(partial_segment())["IsPartial"] is True


def test_a_transcript_is_marked_final():
    assert transform_contact_lens_segment(final_segment())["IsPartial"] is False


def test_successive_partials_for_one_id_accumulate():
    """Contact Lens sends each partial as only the new words, not the whole line."""
    transform_contact_lens_segment(partial_segment(content="hello"))
    second = transform_contact_lens_segment(partial_segment(content="there"))
    assert second["Transcript"] == " hello there"


def test_partials_for_different_ids_do_not_run_together():
    """Two speakers mid-utterance must not have their text spliced."""
    transform_contact_lens_segment(partial_segment("t1", "first line"))
    other = transform_contact_lens_segment(partial_segment("t2", "second line"))
    assert other["Transcript"] == " second line"
    assert "first line" not in other["Transcript"]


def test_a_final_transcript_clears_the_accumulated_partials():
    """Otherwise the dict grows for the life of the Lambda container, and a later
    utterance reusing the id would be prefixed with this meeting's text."""
    transform_contact_lens_segment(partial_segment("t1", "hello"))
    assert "t1" in ep.UTTERANCES_MAP
    transform_contact_lens_segment(final_segment("t1", "hello there"))
    assert "t1" not in ep.UTTERANCES_MAP


def test_a_final_transcript_uses_its_own_content_not_the_accumulation():
    transform_contact_lens_segment(partial_segment("t1", "hel"))
    final = transform_contact_lens_segment(final_segment("t1", "hello there"))
    assert final["Transcript"] == "hello there"
    assert final["OriginalTranscript"] == "hello there"


def test_a_segment_that_is_neither_is_rejected():
    """A category-only segment reaching here means the caller's filter broke."""
    with pytest.raises(ValueError, match="Invalid segment type"):
        transform_contact_lens_segment({"CallId": "contact-1", "Categories": {}})


# ── channel, times and sentiment ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("participant", "expected"),
    [("CUSTOMER", "CALLER"), ("AGENT", "AGENT")],
)
def test_customer_is_renamed_to_caller(participant, expected):
    """Contact Lens says CUSTOMER; the LMA schema knows CALLER."""
    segment = transform_contact_lens_segment(
        final_segment(**{"ParticipantRole": participant})
    )
    assert segment["Channel"] == expected


def test_a_missing_participant_role_defaults_to_agent():
    segment = final_segment()
    del segment["Transcript"]["ParticipantRole"]
    assert transform_contact_lens_segment(segment)["Channel"] == "AGENT"


def test_milliseconds_are_converted_to_seconds():
    """A missed division is 1000x wrong and still renders as a number."""
    segment = transform_contact_lens_segment(
        final_segment(**{"BeginOffsetMillis": 1500, "EndOffsetMillis": 4250})
    )
    assert segment["StartTime"] == 1.5
    assert segment["EndTime"] == 4.25


def test_a_zero_offset_stays_zero():
    """The first segment of a meeting; StartTime is non-nullable in the schema."""
    segment = transform_contact_lens_segment(
        final_segment(**{"BeginOffsetMillis": 0, "EndOffsetMillis": 0})
    )
    assert segment["StartTime"] == 0.0
    assert segment["EndTime"] == 0.0


def test_sentiment_is_carried_with_a_weight_and_a_score():
    segment = transform_contact_lens_segment(final_segment(**{"Sentiment": "NEGATIVE"}))
    assert segment["Sentiment"] == "NEGATIVE"
    assert segment["SentimentWeighted"] == ep.SENTIMENT_WEIGHT["NEGATIVE"]
    assert segment["SentimentWeighted"] < 0
    assert segment["SentimentScore"] == ep.SENTIMENT_SCORE


def test_an_unknown_sentiment_weighs_nothing_rather_than_raising():
    segment = transform_contact_lens_segment(final_segment(**{"Sentiment": "WAT"}))
    assert segment["SentimentWeighted"] == 0


def test_a_segment_without_sentiment_carries_none_of_the_sentiment_fields():
    segment = transform_contact_lens_segment(final_segment())
    assert "Sentiment" not in segment
    assert "SentimentWeighted" not in segment


def test_a_partial_never_carries_sentiment():
    """Scoring half a sentence would be scoring the wrong text."""
    assert "Sentiment" not in transform_contact_lens_segment(partial_segment())


# ── category segments ───────────────────────────────────────────────────────


def test_a_category_spans_all_of_its_points_of_interest():
    """The row's time range is the outer bound across every match, not the first."""
    segment = transform_segment_to_categories_agent_assist(
        category="escalation",
        category_details={
            "PointsOfInterest": [
                {"BeginOffsetMillis": 5000, "EndOffsetMillis": 6000},
                {"BeginOffsetMillis": 1000, "EndOffsetMillis": 2000},
                {"BeginOffsetMillis": 8000, "EndOffsetMillis": 9000},
            ]
        },
        call_id="contact-1",
    )
    assert segment["StartTime"] == 1.0
    assert segment["EndTime"] == 9.0
    assert segment["Transcript"] == "escalation"
    assert segment["Channel"] == "AGENT_ASSISTANT"
    assert segment["IsPartial"] is False
    assert segment["CallId"] == "contact-1"


def test_a_category_with_one_point_of_interest_uses_its_bounds():
    segment = transform_segment_to_categories_agent_assist(
        category="greeting",
        category_details={
            "PointsOfInterest": [{"BeginOffsetMillis": 0, "EndOffsetMillis": 1500}]
        },
        call_id="contact-1",
    )
    assert segment["StartTime"] == 0.0
    assert segment["EndTime"] == 1.5


def test_each_category_row_gets_its_own_segment_id():
    details = {"PointsOfInterest": [{"BeginOffsetMillis": 0, "EndOffsetMillis": 1}]}
    first = transform_segment_to_categories_agent_assist("a", details, "contact-1")
    second = transform_segment_to_categories_agent_assist("a", details, "contact-1")
    assert first["SegmentId"] != second["SegmentId"]


# ── issue segments ──────────────────────────────────────────────────────────


ISSUE_CONTENT = "the widget is broken and I am unhappy"


def issue_segment(begin_char: int = 4, end_char: int = 20) -> dict:
    return {
        "CallId": "contact-1",
        "Transcript": {
            "Id": "t1",
            "Content": ISSUE_CONTENT,
            "BeginOffsetMillis": 2000,
            "EndOffsetMillis": 5000,
            "IssuesDetected": [
                {"CharacterOffsets": {"BeginOffsetChar": begin_char,
                                      "EndOffsetChar": end_char}}
            ],
        },
    }


def test_an_issue_transcript_is_cut_out_by_character_offset():
    """An off-by-one shows up as a truncated sentence, not as an error."""
    segment = issue_segment()
    issue = segment["Transcript"]["IssuesDetected"][0]
    result = transform_segment_to_issues_agent_assist(segment, issue)
    assert result["Transcript"] == "widget is broken"
    assert result["Channel"] == "AGENT_ASSISTANT"


def test_the_issue_slice_is_verbatim_and_not_trimmed():
    """The offsets are Contact Lens's, so the text is taken exactly as given.

    Pinned because trimming would be a reasonable-looking change that would then
    disagree with the offsets the UI uses to highlight the issue in context.
    """
    segment = issue_segment(begin_char=3, end_char=21)
    issue = segment["Transcript"]["IssuesDetected"][0]
    result = transform_segment_to_issues_agent_assist(segment, issue)
    assert result["Transcript"] == " widget is broken "
    assert result["Transcript"] == ISSUE_CONTENT[3:21]


def test_an_issue_ends_a_millisecond_after_its_segment():
    """The nudge is what keeps the issue sorting after the line it came from."""
    segment = issue_segment()
    issue = segment["Transcript"]["IssuesDetected"][0]
    result = transform_segment_to_issues_agent_assist(segment, issue)
    assert result["StartTime"] == 2.0
    assert result["EndTime"] == pytest.approx(5.001)
    assert result["EndTime"] > result["StartTime"]


def test_a_segment_with_no_issues_detected_is_rejected():
    segment = issue_segment()
    issue = segment["Transcript"]["IssuesDetected"][0]
    segment["Transcript"]["IssuesDetected"] = []
    with pytest.raises(ValueError, match="Invalid issue segment"):
        transform_segment_to_issues_agent_assist(segment, issue)


# ── fields every transformed row carries ────────────────────────────────────


@pytest.mark.parametrize("builder", ["contact_lens", "category", "issue"])
def test_every_transform_stamps_the_transcript_ttl_and_status(builder):
    """All three rows go to DynamoDB, so all three need an expiry and a status."""
    if builder == "contact_lens":
        row = transform_contact_lens_segment(final_segment())
    elif builder == "category":
        row = transform_segment_to_categories_agent_assist(
            "a", {"PointsOfInterest": [{"BeginOffsetMillis": 0, "EndOffsetMillis": 1}]},
            "contact-1",
        )
    else:
        segment = issue_segment()
        row = transform_segment_to_issues_agent_assist(
            segment, segment["Transcript"]["IssuesDetected"][0]
        )
    assert row["Status"] == "TRANSCRIBING"
    assert isinstance(row["ExpiresAfter"], int)
    assert row["ExpiresAfter"] > 0
    assert row["CallId"] == "contact-1"
    assert row["CreatedAt"]
