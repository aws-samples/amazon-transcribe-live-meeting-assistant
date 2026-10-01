# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for transcript-segment normalisation and the DynamoDB TTL it stamps.

`normalize_transcript_segments` is the funnel every transcript on the Kinesis
stream passes through on its way to the `addTranscriptSegment` mutation, for all
four producers: Amazon Transcribe streaming (`TranscriptEvent`), Transcribe Call
Analytics (`UtteranceEvent`), Contact Lens (`ContactId`), and the custom shape the
WebSocket transcriber and the MicroVM ASR engine emit. Everything it gets wrong
fails at the AppSync boundary, one record at a time, for reasons that surface as
missing transcript lines rather than as an error anyone sees.

Two areas are covered because both have misbehaved in production:

* **Zero timestamps.** The schema declares `StartTime: Float!` and
  `EndTime: Float!`, so a `None` is rejected and the record is lost. A segment
  beginning at 0.0 — the first segment of every meeting — is the case to watch,
  because a truthiness test on the incoming value cannot tell it from absent.
* **The TTL.** `ExpiresAfter` decides when DynamoDB deletes the row, so these
  assert the *value* it is given, not merely that the field is present. A test
  that only checks presence passes against a TTL of any length, including one
  that expires rows immediately.

No AWS calls: nothing here reaches Comprehend or Cognito.
"""

from __future__ import annotations

import importlib
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import eventprocessor_utils.eventprocessor as ep

# The record shape shared by the custom-producer cases; each test overrides the
# fields it is about.
CUSTOM_BASE = {"CallId": "call-1", "Transcript": "hello there", "IsPartial": False}


def normalize_one(message: dict) -> dict:
    """Normalise a message expected to yield exactly one segment."""
    segments = ep.normalize_transcript_segments(dict(message))
    assert len(segments) == 1, f"expected one segment, got {len(segments)}"
    return segments[0]


# ── the DynamoDB TTL ────────────────────────────────────────────────────────


def test_get_ttl_is_the_requested_number_of_days_ahead():
    """The value, not just its presence: a wrong TTL deletes rows early."""
    before = datetime.now(timezone.utc)
    ttl = ep.get_ttl("30")
    expected = (before + timedelta(days=30)).timestamp()
    assert isinstance(ttl, int), "DynamoDB TTL must be an integer epoch second"
    # A couple of minutes of slack covers a slow machine without admitting a
    # value that is out by an hour, a day, or a unit.
    assert abs(ttl - expected) < 120, f"TTL is {(ttl - expected) / 86400:.3f} days out"


def test_get_ttl_accepts_a_fractional_number_of_days():
    """Retention shorter than a day is expressible, and is not truncated to zero."""
    before = datetime.now(timezone.utc)
    ttl = ep.get_ttl("0.5")
    expected = (before + timedelta(hours=12)).timestamp()
    assert abs(ttl - expected) < 120


@pytest.mark.parametrize("days", ["1", "7", "90", "365"])
def test_get_ttl_scales_with_the_configured_retention(days):
    now = datetime.now(timezone.utc).timestamp()
    assert abs((ep.get_ttl(days) - now) / 86400 - float(days)) < 0.01


def test_meeting_and_transcription_ttls_read_their_own_settings(monkeypatch):
    """The two retention periods are configured separately and must not be crossed.

    Both are read into module globals at import time, so the module is reloaded
    here rather than only the environment being set — which is also what makes
    this worth asserting: a refactor that reads the variable at call time instead
    would still pass, and one that swaps the two would not.
    """
    monkeypatch.setenv("MEETING_RECORD_EXPIRATION_IN_DAYS", "10")
    monkeypatch.setenv("TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS", "20")
    reloaded = importlib.reload(ep)
    try:
        now = datetime.now(timezone.utc).timestamp()
        assert abs((reloaded.get_meeting_ttl() - now) / 86400 - 10) < 0.01
        assert abs((reloaded.get_transcription_ttl() - now) / 86400 - 20) < 0.01
    finally:
        # Restore the module for every other test in the session.
        monkeypatch.undo()
        importlib.reload(ep)


def test_segment_expiry_uses_the_transcription_retention(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS", "3")
    monkeypatch.setenv("MEETING_RECORD_EXPIRATION_IN_DAYS", "99")
    reloaded = importlib.reload(ep)
    try:
        now = datetime.now(timezone.utc).timestamp()
        segment = reloaded.normalize_transcript_segments(
            {**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0}
        )[0]
        days = (segment["ExpiresAfter"] - now) / 86400
        assert abs(days - 3) < 0.01, f"segment TTL is {days:.2f} days, expected 3"
    finally:
        monkeypatch.undo()
        importlib.reload(ep)


def test_segment_expiry_is_recomputed_and_not_taken_from_the_producer():
    """Retention is the stack's setting, so a value on the wire does not win.

    Asserted deliberately: a producer that sent a stale or hostile `ExpiresAfter`
    would otherwise control how long transcripts are kept.
    """
    segment = normalize_one({**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0,
                            "ExpiresAfter": 12345})
    assert segment["ExpiresAfter"] != 12345
    assert segment["ExpiresAfter"] > datetime.now(timezone.utc).timestamp()


# ── zero timestamps, per producer ───────────────────────────────────────────
#
# The first segment of a meeting starts at 0.0. Each producer shape is checked
# separately because each reads the timestamps by a different route.


@pytest.mark.parametrize(
    ("start", "end"),
    [(0.0, 0.0), (0.0, 1.5), (0, 0), (0.0, 0.25)],
    ids=["both-zero", "start-zero", "int-zeros", "start-zero-short"],
)
def test_custom_producer_keeps_zero_start_and_end_times(start, end):
    segment = normalize_one({**CUSTOM_BASE, "StartTime": start, "EndTime": end})
    assert segment["StartTime"] == start
    assert segment["EndTime"] == end


def test_custom_producer_keeps_zero_millisecond_offsets():
    """`BeginOffsetMillis`/`EndOffsetMillis` are the alternative spelling."""
    segment = normalize_one({**CUSTOM_BASE, "BeginOffsetMillis": 0, "EndOffsetMillis": 0})
    assert segment["StartTime"] == 0
    assert segment["EndTime"] == 0


def test_custom_producer_prefers_explicit_times_over_offsets():
    """Both spellings present: the explicit times win, including when they are 0."""
    segment = normalize_one({
        **CUSTOM_BASE,
        "BeginOffsetMillis": 5000, "EndOffsetMillis": 9000,
        "StartTime": 0.0, "EndTime": 1.0,
    })
    assert segment["StartTime"] == 0.0
    assert segment["EndTime"] == 1.0


def test_transcribe_streaming_event_keeps_zero_times():
    segment = normalize_one({
        "CallId": "call-1",
        "TranscriptEvent": {
            "Channel": "AGENT", "ResultId": "r1", "StartTime": 0.0, "EndTime": 0.0,
            "Transcript": "hello", "IsPartial": False,
        },
    })
    assert segment["StartTime"] == 0.0
    assert segment["EndTime"] == 0.0


def test_call_analytics_utterance_event_keeps_zero_offsets():
    segment = normalize_one({
        "CallId": "call-1",
        "UtteranceEvent": {
            "ParticipantRole": "AGENT", "UtteranceId": "u1",
            "BeginOffsetMillis": 0, "EndOffsetMillis": 0,
            "Transcript": "hello", "IsPartial": False,
        },
    })
    assert segment["StartTime"] == 0.0
    assert segment["EndTime"] == 0.0


def test_call_analytics_offsets_are_converted_to_seconds():
    segment = normalize_one({
        "CallId": "call-1",
        "UtteranceEvent": {
            "ParticipantRole": "AGENT", "UtteranceId": "u1",
            "BeginOffsetMillis": 1500, "EndOffsetMillis": 4250,
            "Transcript": "hello", "IsPartial": False,
        },
    })
    assert segment["StartTime"] == 1.5
    assert segment["EndTime"] == 4.25


# ── channel, speaker and identity mapping ───────────────────────────────────


@pytest.mark.parametrize(
    ("producer_channel", "expected"),
    [("CUSTOMER", "CALLER"), ("AGENT", "AGENT"), ("CALLER", "CALLER")],
)
def test_transcribe_streaming_customer_channel_is_renamed_to_caller(
    producer_channel, expected
):
    """LMA's schema knows CALLER/AGENT; Transcribe Call Analytics says CUSTOMER."""
    segment = normalize_one({
        "CallId": "call-1",
        "TranscriptEvent": {
            "Channel": producer_channel, "ResultId": "r1",
            "StartTime": 1.0, "EndTime": 2.0,
            "Transcript": "hello", "IsPartial": False,
        },
    })
    assert segment["Channel"] == expected


def test_transcribe_streaming_without_a_speaker_gets_a_placeholder():
    segment = normalize_one({
        "CallId": "call-1",
        "TranscriptEvent": {
            "Channel": "AGENT", "ResultId": "r1", "StartTime": 1.0, "EndTime": 2.0,
            "Transcript": "hello", "IsPartial": False,
        },
    })
    assert segment["Speaker"] == "Other Participant"


def test_custom_producer_channel_follows_is_caller():
    caller = normalize_one({**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0,
                            "IsCaller": True})
    agent = normalize_one({**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0,
                           "IsCaller": False})
    assert caller["Channel"] == "CALLER"
    assert agent["Channel"] == "AGENT"


def test_custom_producer_explicit_channel_wins_over_is_caller():
    segment = normalize_one({**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0,
                             "Channel": "AGENT", "IsCaller": True})
    assert segment["Channel"] == "AGENT"


def test_custom_producer_gets_a_generated_segment_id_when_none_is_sent():
    """Two segments without an id must not collide on the DynamoDB sort key."""
    first = normalize_one({**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0})
    second = normalize_one({**CUSTOM_BASE, "StartTime": 2.0, "EndTime": 3.0})
    assert first["SegmentId"] and second["SegmentId"]
    assert first["SegmentId"] != second["SegmentId"]


def test_custom_producer_keeps_the_segment_id_it_is_given():
    segment = normalize_one({**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0,
                             "SegmentId": "seg-42"})
    assert segment["SegmentId"] == "seg-42"


# ── partial segments and sentiment ──────────────────────────────────────────


def test_call_analytics_partial_segment_carries_no_sentiment():
    """Sentiment on an incomplete utterance would be scored on half a sentence."""
    segment = normalize_one({
        "CallId": "call-1",
        "UtteranceEvent": {
            "ParticipantRole": "AGENT", "UtteranceId": "u1",
            "BeginOffsetMillis": 0, "EndOffsetMillis": 500,
            "Transcript": "hello", "IsPartial": True,
            "Sentiment": "POSITIVE", "SentimentWeighted": 5,
            "SentimentScore": {"Positive": 0.9},
        },
    })
    assert segment["IsPartial"] is True
    assert segment["Sentiment"] is None
    assert segment["SentimentWeighted"] is None
    assert segment["SentimentScore"] is None


def test_call_analytics_final_segment_keeps_its_sentiment():
    segment = normalize_one({
        "CallId": "call-1",
        "UtteranceEvent": {
            "ParticipantRole": "AGENT", "UtteranceId": "u1",
            "BeginOffsetMillis": 0, "EndOffsetMillis": 500,
            "Transcript": "hello", "IsPartial": False,
            "Sentiment": "POSITIVE", "SentimentWeighted": 5,
            "SentimentScore": {"Positive": 0.9},
        },
    })
    assert segment["Sentiment"] == "POSITIVE"
    assert segment["SentimentWeighted"] == 5
    assert segment["SentimentScore"] == {"Positive": 0.9}


# ── fields every segment carries ────────────────────────────────────────────


def test_every_segment_carries_the_fields_the_mutation_requires():
    """A missing key is a mutation-time failure, so the shape is pinned here."""
    segment = normalize_one({**CUSTOM_BASE, "StartTime": 0.0, "EndTime": 1.0})
    for field in (
        "CallId", "Channel", "SegmentId", "StartTime", "EndTime", "Transcript",
        "IsPartial", "Status", "ExpiresAfter", "CreatedAt",
    ):
        assert field in segment, f"{field} missing from the normalised segment"
    assert segment["StartTime"] is not None
    assert segment["EndTime"] is not None


def test_a_new_segment_starts_in_the_transcribing_status():
    assert normalize_one({**CUSTOM_BASE, "StartTime": 1.0,
                          "EndTime": 2.0})["Status"] == "TRANSCRIBING"


def test_created_at_is_an_offset_aware_iso_timestamp():
    """The schema types CreatedAt as AWSDateTime, which requires the offset."""
    created = normalize_one({**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0})["CreatedAt"]
    assert re.search(r"([+-]\d{2}:\d{2}|Z)$", created), created
    assert datetime.fromisoformat(created).tzinfo is not None


def test_the_original_transcript_is_kept_alongside_the_transcript():
    """Redaction and translation overwrite Transcript; the original is the fallback."""
    segment = normalize_one({**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0})
    assert segment["Transcript"] == "hello there"
    assert segment["OriginalTranscript"] == "hello there"


def test_an_unset_access_token_leaves_the_owner_unset():
    """No token means no owner; it must not become the string 'None'."""
    segment = normalize_one({**CUSTOM_BASE, "StartTime": 1.0, "EndTime": 2.0})
    assert segment.get("Owner") in (None, "")


# ── Contact Lens ────────────────────────────────────────────────────────────


def test_contact_lens_categories_do_not_become_transcript_segments():
    """Category segments are the agent-assist path's business, not transcript rows."""
    segments = ep.normalize_transcript_segments({
        "ContactId": "contact-1",
        "Segments": [
            {"Categories": {"MatchedCategories": ["escalation"]}},
            {"Transcript": {"Id": "t1", "ParticipantId": "AGENT",
                            "BeginOffsetMillis": 0, "EndOffsetMillis": 1000,
                            "Content": "hello"}},
        ],
    })
    assert len(segments) == 1
    assert segments[0]["CallId"] == "contact-1"


def test_os_environment_is_not_mutated_by_importing_the_module():
    """Guards the reload-based TTL tests from leaking settings into the rest."""
    assert os.environ.get("TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS") in (None, "90", "")
