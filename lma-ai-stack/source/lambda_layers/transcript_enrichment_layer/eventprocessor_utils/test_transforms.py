# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the Contact Lens transform in eventprocessor_utils.

`transform_contact_lens_segment` turns a Contact Lens payload into the shape the
AppSync mutation takes: a transcript row, from either a partial (`Utterance`) or
a final (`Transcript`) segment. Partials are **accumulated in a module-level
dict**, `UTTERANCES_MAP`, and removed when the final for that id arrives; get that
wrong and a meeting either loses text or repeats it.

Contact Lens reports times in milliseconds and the rest of LMA works in seconds,
so the transform divides by 1000 — the conversion is asserted explicitly because
a missed division is three orders of magnitude wrong and still renders.

No AWS: it is a pure function over a dict.
"""

from __future__ import annotations

import pytest

import eventprocessor_utils.eventprocessor as ep
from eventprocessor_utils.eventprocessor import transform_contact_lens_segment


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
    """A segment with neither an utterance nor a transcript has no text to carry."""
    with pytest.raises(ValueError, match="Invalid segment type"):
        transform_contact_lens_segment({"CallId": "contact-1", "Categories": {}})


# ── channel, times and sentiment ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("participant", "expected"),
    [("CUSTOMER", "CALLER"), ("AGENT", "AGENT")],
)
def test_customer_is_renamed_to_caller(participant, expected):
    """Contact Lens says CUSTOMER; the LMA schema knows CALLER."""
    segment = transform_contact_lens_segment(final_segment(**{"ParticipantRole": participant}))
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


# ── fields every transformed row carries ────────────────────────────────────


def test_the_transform_stamps_the_transcript_ttl_and_status():
    """The row goes to DynamoDB, so it needs an expiry and a status."""
    row = transform_contact_lens_segment(final_segment())
    assert row["Status"] == "TRANSCRIBING"
    assert isinstance(row["ExpiresAfter"], int)
    assert row["ExpiresAfter"] > 0
    assert row["CallId"] == "contact-1"
    assert row["CreatedAt"]


class _Comprehend:
    def detect_sentiment(self, **_kwargs):
        return {"Sentiment": "NEUTRAL", "SentimentScore": {"Neutral": 1.0}}


def test_each_comprehend_request_is_reported_for_metering() -> None:
    import asyncio

    seen = []
    args = {
        "comprehend_client": _Comprehend(),
        "on_comprehend_request": lambda call_id, text: seen.append((call_id, text)),
    }
    message = {"CallId": "m-1", "Transcript": "we ship on friday"}
    result = asyncio.run(ep.transform_segment_to_add_sentiment(message, args))
    assert result["Sentiment"] == "NEUTRAL"
    assert seen == [("m-1", "we ship on friday")]


def test_a_failing_metering_hook_does_not_cost_the_segment_its_sentiment() -> None:
    import asyncio

    def broken(_call_id, _text):
        raise RuntimeError("table unavailable")

    args = {"comprehend_client": _Comprehend(), "on_comprehend_request": broken}
    result = asyncio.run(
        ep.transform_segment_to_add_sentiment({"CallId": "m-1", "Transcript": "x"}, args)
    )
    assert result["Sentiment"] == "NEUTRAL"


def test_a_supplied_sentiment_label_is_not_metered() -> None:
    import asyncio

    seen = []
    args = {"on_comprehend_request": lambda *a: seen.append(a)}
    asyncio.run(
        ep.transform_segment_to_add_sentiment(
            {"CallId": "m-1", "Transcript": "x", "Sentiment": "POSITIVE"}, args
        )
    )
    assert seen == []
