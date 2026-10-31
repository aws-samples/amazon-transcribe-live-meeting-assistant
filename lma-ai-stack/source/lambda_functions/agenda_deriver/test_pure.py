# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The judgement behind deriving an agenda: never store one that was not stated."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pure  # noqa: E402


def seg(start, text, speaker="Ann", channel="AGENT", partial=False):
    return {
        "StartTime": start,
        "EndTime": start + 2,
        "Transcript": text,
        "Speaker": speaker,
        "Channel": channel,
        "IsPartial": partial,
    }


STATED = [
    seg(1, "Morning everyone."),
    seg(5, "Today we have three things to get through: the budget, hiring, and the Q3 plan."),
    seg(12, "Sounds good.", speaker="Bob", channel="CALLER"),
]
TRANSCRIPT = pure.build_transcript(STATED)


def answer(**fields):
    return json.dumps({"agenda_stated": True, "items": [], "evidence": "", **fields})


# ── the transcript the model reads ──────────────────────────────────────────


def test_the_transcript_is_final_speech_in_order_with_times():
    segments = [
        seg(65, "Second.", speaker="Bob", channel="CALLER"),
        seg(3, "First."),
        seg(30, "draft words", partial=True),
        seg(40, "The assistant said this.", channel="AGENT_ASSISTANT"),
        seg(50, "   "),
    ]
    assert pure.build_transcript(segments) == "[00:03] Ann: First.\n[01:05] Bob: Second."


def test_a_long_meeting_keeps_its_opening():
    segments = [seg(i, "word " * 50) for i in range(2000)]
    transcript = pure.build_transcript(segments)
    assert len(transcript) == pure.MAX_TRANSCRIPT_CHARS
    assert transcript.startswith("[00:00] Ann:")


def test_the_prompt_asks_for_a_stated_agenda_and_says_none_is_normal():
    prompt = pure.build_prompt("[00:01] Ann: hello")
    assert "[00:01] Ann: hello" in prompt
    assert "NOT an agenda" in prompt
    assert '"agenda_stated": false' in prompt


# ── what is accepted ────────────────────────────────────────────────────────


def test_a_stated_agenda_with_real_evidence_is_accepted():
    raw = answer(
        items=["the budget", "hiring", "the Q3 plan"],
        evidence="Today we have three things to get through: the budget, hiring, and the Q3 plan.",
    )
    assert pure.parse_model_response(raw, TRANSCRIPT) == ["the budget", "hiring", "the Q3 plan"]


def test_evidence_is_matched_ignoring_case_punctuation_and_spacing():
    raw = answer(
        items=["budget"], evidence="today we have THREE things to get through -- the budget"
    )
    assert pure.parse_model_response(raw, TRANSCRIPT) == ["budget"]


def test_evidence_spanning_two_segments_still_matches():
    segments = [seg(1, "The agenda today is the budget"), seg(4, "and then hiring.")]
    transcript = pure.build_transcript(segments)
    raw = answer(
        items=["budget", "hiring"], evidence="the agenda today is the budget and then hiring"
    )
    assert pure.parse_model_response(raw, transcript) == ["budget", "hiring"]


def test_an_agenda_whose_evidence_is_not_in_the_transcript_is_discarded():
    """The guard against an invented agenda: it has nothing real to quote."""
    raw = answer(items=["Budget", "Hiring"], evidence="Our agenda is budget and hiring.")
    assert pure.parse_model_response(raw, TRANSCRIPT) is None


def test_a_token_quote_is_not_evidence():
    raw = answer(items=["Budget"], evidence="the budget")
    assert pure.parse_model_response(raw, TRANSCRIPT) is None


def test_not_stated_and_unreadable_answers_store_nothing():
    for raw in (
        json.dumps({"agenda_stated": False, "items": ["x"], "evidence": "Morning everyone."}),
        "I could not find one.",
        "",
        "[1, 2]",
        json.dumps(
            {"agenda_stated": "true", "items": ["budget"], "evidence": "Today we have three"}
        ),
    ):
        assert pure.parse_model_response(raw, TRANSCRIPT) is None, raw


def test_json_wrapped_in_prose_is_read():
    raw = "Here you go:\n" + answer(
        items=["hiring"], evidence="three things to get through: the budget, hiring"
    )
    assert pure.parse_model_response(raw, TRANSCRIPT) == ["hiring"]


def test_items_are_cleaned_deduplicated_and_bounded():
    evidence = "three things to get through: the budget, hiring"
    raw = answer(
        items=["  the   budget ", "the budget", 7, "", "x" * 500]
        + [f"item {i}" for i in range(40)],
        evidence=evidence,
    )
    items = pure.parse_model_response(raw, TRANSCRIPT)
    assert items[0] == "the budget"
    assert len(items[1]) == pure.MAX_ITEM_CHARS
    assert len(items) == pure.MAX_ITEMS
    assert items.count("the budget") == 1


def test_an_empty_item_list_stores_nothing():
    raw = answer(items=[], evidence="three things to get through: the budget, hiring")
    assert pure.parse_model_response(raw, TRANSCRIPT) is None


def test_the_stored_form_is_one_item_per_line():
    assert pure.agenda_text(["a", "b"]) == "a\nb"
    assert pure.has_agenda({"Agenda": " a "}) is True
    assert pure.has_agenda({"Agenda": " "}) is False
    assert pure.has_agenda(None) is False
