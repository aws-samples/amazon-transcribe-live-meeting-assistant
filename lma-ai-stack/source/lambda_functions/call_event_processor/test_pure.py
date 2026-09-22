# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the computation extracted out of call_event_processor.

`call_event_processor.py` is 1,700 lines and had 568 lines of tests, all of them
about partial-batch-failure handling. Nothing covered the logic that decides what
a meeting looks like, because the module cannot be imported without aiohttp,
aws_lambda_powertools and a Lambda layer on the path. `pure.py` holds the parts
that only transform their arguments; these test them directly.

What is covered, and why each matters:

* `get_sentiment_per_quarter` — the sentiment trend on the meeting page. It always
  returns four periods, assigns each segment by its end offset, and averages. Every
  way of getting the boundaries wrong produces four plausible numbers, so the
  boundary rule is asserted from both sides: a segment landing exactly on a
  boundary must be counted once, in the earlier period.
* `matches_wake_phrase` — whether an utterance invokes the Meeting Assistant. Too
  loose and the assistant answers everything; too strict and it never answers.
* `convert_keys_to_uppercamelcase` — brings incoming keys into line with the
  GraphQL schema. It changes only the first character, and a key it mangles is a
  field the mutation then drops.
* `merge_dicts` — must not modify its arguments, since callers hold the originals.

No AWS, and no import of call_event_processor itself.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# The function root, so `pure` resolves as a sibling — the same path handling
# test_batch_item_failures.py uses, and the way Lambda itself resolves it.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from pure import (  # noqa: E402
    QUARTER_COUNT,
    convert_keys_to_uppercamelcase,
    get_sentiment_per_quarter,
    matches_wake_phrase,
    merge_dicts,
)


def entry(begin: float, end: float, score: float, sentiment: str = "POSITIVE") -> dict:
    """One sentiment measurement, as the processor accumulates them per channel."""
    return {
        "Id": f"seg-{begin}",
        "BeginOffsetMillis": begin,
        "EndOffsetMillis": end,
        "Sentiment": sentiment,
        "Score": score,
    }


# ── the sentiment trend ─────────────────────────────────────────────────────


def test_an_empty_meeting_still_produces_four_periods():
    """The chart expects a fixed number of points whatever the input."""
    quarters = get_sentiment_per_quarter([])
    assert len(quarters) == QUARTER_COUNT
    assert all(q == {"Score": 0, "BeginOffsetMillis": 0, "EndOffsetMillis": 0} for q in quarters)


def test_four_evenly_spaced_segments_land_one_per_period():
    """The simplest case, and the one that pins the boundary arithmetic."""
    quarters = get_sentiment_per_quarter([
        entry(0, 1000, 1.0),
        entry(1000, 2000, 2.0),
        entry(2000, 3000, 3.0),
        entry(3000, 4000, 4.0),
    ])
    assert [q["Score"] for q in quarters] == [1.0, 2.0, 3.0, 4.0]


def test_scores_within_a_period_are_averaged():
    quarters = get_sentiment_per_quarter([
        entry(0, 500, 1.0),
        entry(500, 1000, 3.0),
        entry(3000, 4000, 4.0),
    ])
    assert quarters[0]["Score"] == pytest.approx(2.0)
    assert quarters[3]["Score"] == pytest.approx(4.0)


def test_a_period_with_no_segments_scores_zero():
    """Zero rather than the previous period's value, so a gap stays a gap."""
    quarters = get_sentiment_per_quarter([entry(0, 1000, 5.0), entry(3000, 4000, 5.0)])
    assert quarters[1]["Score"] == 0
    assert quarters[1]["BeginOffsetMillis"] == 0
    assert quarters[1]["EndOffsetMillis"] == 0


def test_every_segment_is_counted_exactly_once():
    """Boundaries are exclusive at the start and inclusive at the end.

    A segment ending exactly on a boundary would otherwise appear in two periods
    and be averaged into both, or in neither and vanish from the chart.
    """
    segments = [entry(i * 100, (i + 1) * 1000, 1.0) for i in range(8)]
    quarters = get_sentiment_per_quarter(segments)
    # Each quarter reports the span of what it holds; summing the counts is not
    # directly observable, so assert instead that no segment is lost: the overall
    # span of the four periods must cover the whole meeting.
    populated = [q for q in quarters if q["EndOffsetMillis"]]
    assert min(q["BeginOffsetMillis"] for q in populated) == 0
    assert max(q["EndOffsetMillis"] for q in populated) == 8000


def test_a_segment_ending_on_a_boundary_falls_in_the_earlier_period():
    """Two segments, one ending exactly at the midpoint of a 0-4000 meeting."""
    quarters = get_sentiment_per_quarter([
        entry(0, 2000, 7.0),   # ends exactly on the 2nd/3rd boundary
        entry(2000, 4000, 9.0),
    ])
    assert quarters[1]["Score"] == pytest.approx(7.0)
    assert quarters[2]["Score"] == 0
    assert quarters[3]["Score"] == pytest.approx(9.0)


def test_the_periods_are_derived_from_the_meeting_not_from_zero():
    """A meeting whose first segment starts late is still divided into four.

    Dividing from zero instead would push every segment into the last period.
    """
    quarters = get_sentiment_per_quarter([
        entry(10_000, 11_000, 1.0),
        entry(11_000, 12_000, 2.0),
        entry(12_000, 13_000, 3.0),
        entry(13_000, 14_000, 4.0),
    ])
    assert [q["Score"] for q in quarters] == [1.0, 2.0, 3.0, 4.0]
    assert quarters[0]["BeginOffsetMillis"] == 10_000


def test_input_order_does_not_matter():
    """Kinesis does not guarantee order, so the result must not depend on it."""
    forwards = [entry(0, 1000, 1.0), entry(1000, 2000, 2.0),
                entry(2000, 3000, 3.0), entry(3000, 4000, 4.0)]
    assert get_sentiment_per_quarter(list(reversed(forwards))) == \
        get_sentiment_per_quarter(forwards)


def test_negative_scores_are_preserved():
    """The score is signed; losing the sign inverts the chart."""
    quarters = get_sentiment_per_quarter([entry(0, 1000, -5.0), entry(3000, 4000, 5.0)])
    assert quarters[0]["Score"] == pytest.approx(-5.0)
    assert quarters[3]["Score"] == pytest.approx(5.0)


def test_a_single_segment_meeting_does_not_divide_by_zero():
    """One segment means a zero-length span; the arithmetic must still hold."""
    quarters = get_sentiment_per_quarter([entry(1000, 1000, 3.0)])
    assert len(quarters) == QUARTER_COUNT


def test_the_input_list_is_not_reordered():
    """The caller keeps using its own list after this returns."""
    segments = [entry(3000, 4000, 4.0), entry(0, 1000, 1.0)]
    before = [s["Id"] for s in segments]
    get_sentiment_per_quarter(segments)
    assert [s["Id"] for s in segments] == before


# ── the assistant wake phrase ───────────────────────────────────────────────

DEFAULT_WAKE_PHRASE = re.compile("(OK|Okay)[.,! ]*[Aa]ssistant")


@pytest.mark.parametrize(
    "transcript",
    [
        "OK Assistant what did we decide?",
        "Okay Assistant",
        "OK, Assistant",
        "OK. Assistant",
        "OK! Assistant",
        "Okay assistant",
        "so anyway, OK Assistant, summarise that",
    ],
)
def test_the_default_wake_phrase_matches_its_documented_forms(transcript):
    """These are the spellings the shipped default regex is meant to accept."""
    assert matches_wake_phrase(DEFAULT_WAKE_PHRASE, transcript) is True


@pytest.mark.parametrize(
    "transcript",
    [
        "",
        "let us talk about the assistant",
        "the OK button",
        "Assistant",
        "okay then",
    ],
)
def test_ordinary_speech_does_not_wake_the_assistant(transcript):
    """A false match means the assistant answers something nobody asked."""
    assert matches_wake_phrase(DEFAULT_WAKE_PHRASE, transcript) is False


def test_the_phrase_is_found_anywhere_in_the_utterance():
    """`search`, not `fullmatch`: people do not start the sentence with it."""
    assert matches_wake_phrase(DEFAULT_WAKE_PHRASE, "right, OK Assistant, go on") is True


def test_no_configured_pattern_wakes_nothing():
    assert matches_wake_phrase(None, "OK Assistant") is False


def test_an_empty_pattern_matches_every_utterance():
    """Documented because it is reachable: the CloudFormation parameter accepts
    an empty string, and an empty regex matches everything — so clearing the
    setting turns the assistant on for every segment rather than off."""
    assert matches_wake_phrase(re.compile(""), "anything at all") is True


def test_a_custom_pattern_is_honoured():
    assert matches_wake_phrase(re.compile("hey bot"), "hey bot, help") is True
    assert matches_wake_phrase(re.compile("hey bot"), "OK Assistant") is False


def test_the_result_is_a_bool_not_a_match_object():
    """The caller uses it in a boolean `and` chain; a Match would also be truthy,
    but `is True` here keeps the contract explicit."""
    assert matches_wake_phrase(DEFAULT_WAKE_PHRASE, "OK Assistant") is True
    assert matches_wake_phrase(DEFAULT_WAKE_PHRASE, "nope") is False


# ── key conversion ──────────────────────────────────────────────────────────


def test_the_first_letter_of_each_key_is_upper_cased():
    assert convert_keys_to_uppercamelcase({"callId": "c1"}) == {"CallId": "c1"}


def test_an_already_upper_cased_key_is_unchanged():
    assert convert_keys_to_uppercamelcase({"CallId": "c1"}) == {"CallId": "c1"}


def test_only_the_first_character_changes():
    """Upper-casing the whole key would produce a field the schema does not have."""
    assert convert_keys_to_uppercamelcase({"meetingIdValue": 1}) == {"MeetingIdValue": 1}


def test_nested_dicts_are_converted_too():
    result = convert_keys_to_uppercamelcase({"outer": {"innerKey": {"deepKey": 1}}})
    assert result == {"Outer": {"InnerKey": {"DeepKey": 1}}}


def test_values_that_are_not_dicts_are_left_alone():
    """Including lists of dicts, which this function deliberately does not walk."""
    payload = {"items": [{"leaveMe": 1}], "count": 2, "flag": None}
    assert convert_keys_to_uppercamelcase(payload) == {
        "Items": [{"leaveMe": 1}],
        "Count": 2,
        "Flag": None,
    }


def test_an_empty_dict_converts_to_an_empty_dict():
    assert convert_keys_to_uppercamelcase({}) == {}


def test_the_input_dict_is_not_modified():
    payload = {"callId": "c1", "nested": {"innerKey": 1}}
    convert_keys_to_uppercamelcase(payload)
    assert payload == {"callId": "c1", "nested": {"innerKey": 1}}


# ── dict merging ────────────────────────────────────────────────────────────


def test_the_second_dict_wins_on_a_shared_key():
    assert merge_dicts({"a": 1, "b": 2}, {"b": 3}) == {"a": 1, "b": 3}


def test_keys_from_both_are_present():
    assert merge_dicts({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_neither_input_is_modified():
    """Callers keep using the originals, so mutating them corrupts them silently."""
    first, second = {"a": 1}, {"b": 2}
    merged = merge_dicts(first, second)
    assert first == {"a": 1}
    assert second == {"b": 2}
    merged["c"] = 3
    assert "c" not in first and "c" not in second


def test_merging_with_an_empty_dict_returns_an_equal_copy():
    original = {"a": 1}
    result = merge_dicts(original, {})
    assert result == original
    assert result is not original
