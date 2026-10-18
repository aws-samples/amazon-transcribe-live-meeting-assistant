# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the speaker-naming policy.

What is being pinned here is not "does the model work" -- it is that a model
answer cannot do damage. The module asks a model only for a name, a kind of
evidence, and the turn number holding it, then recomputes the attribution from
the turn index itself. So the tests that matter most are the ones that feed in a
WRONG or hostile proposal and assert that nothing is applied.

Live evidence informs two of these. Streaming a four-voice synthetic meeting
through a real deployment produced a transcript where the first seconds of one
speaker's turn had been absorbed into the previous speaker's run -- so an
addressed-by-name inference in that region would have named the wrong person.
That is why addressed-by-name is only ever `proposed`, and why the same-voice
case below is dropped rather than applied.
"""

from __future__ import annotations

import json

import pure
import pytest

NOW = "2026-10-18T03:00:00Z"
SPK0 = "CALLER#Other Participant (spk_0)"
SPK1 = "CALLER#Other Participant (spk_1)"
SPK2 = "CALLER#Other Participant (spk_2)"


def seg(speaker, start, text, channel="CALLER", segment_id=None, partial=False):
    return {
        "Channel": channel,
        "Speaker": speaker,
        "Transcript": text,
        "StartTime": start,
        "EndTime": start + 3,
        "SegmentId": segment_id or f"s{start}",
        "IsPartial": partial,
    }


def empty():
    return {"version": 1, "candidates": [], "identities": {}, "merges": {}, "segmentOverrides": {}}


# A short meeting that contains one of each kind of evidence.
MEETING = [
    seg("Other Participant (spk_0)", 0, "Hi everyone, I'm Sarah Lee and I run the platform team"),
    seg("Other Participant (spk_1)", 5, "I own the data ingestion side of things"),
    seg("Other Participant (spk_0)", 10, "We are on track for the October release"),
    seg("Other Participant (spk_1)", 15, "That is a good point, Sarah, I agree"),
]


def turns():
    return pure.build_turns(MEETING)


def by_number():
    return {t["turn"]: t for t in turns()}


# --------------------------------------------------------------------------
# Turn construction
# --------------------------------------------------------------------------


def test_turns_are_numbered_in_spoken_order() -> None:
    """Turn numbers are what attribution is recomputed from, so the order is
    load-bearing rather than cosmetic."""
    numbered = [(t["turn"], t["key"]) for t in turns()]
    assert numbered == [(1, SPK0), (2, SPK1), (3, SPK0), (4, SPK1)]


def test_turn_order_does_not_depend_on_the_order_segments_were_stored() -> None:
    shuffled = [MEETING[2], MEETING[0], MEETING[3], MEETING[1]]
    assert [t["turn"] for t in pure.build_turns(shuffled)] == [1, 2, 3, 4]
    assert [t["key"] for t in pure.build_turns(shuffled)] == [SPK0, SPK1, SPK0, SPK1]


def test_segments_sharing_a_start_time_are_ordered_deterministically() -> None:
    """Two passes over the same data must number turns identically, or the model's
    cited turn number would point somewhere else on verification."""
    tied = [seg("A (spk_0)", 1, "first", segment_id="b"), seg("A (spk_1)", 1, "second", segment_id="a")]
    first = [t["segment_id"] for t in pure.build_turns(tied)]
    second = [t["segment_id"] for t in pure.build_turns(list(reversed(tied)))]
    assert first == second


def test_partial_segments_are_excluded() -> None:
    """A partial has no voice id yet, so naming its label would name a label that
    is about to change."""
    with_partial = MEETING + [seg("Other Participant", 20, "still speaking", partial=True)]
    assert len(pure.build_turns(with_partial)) == len(MEETING)


def test_non_human_channels_and_empty_text_are_excluded() -> None:
    noise = MEETING + [
        seg("", 20, "assistant reply", channel="AGENT_ASSISTANT"),
        seg("Other Participant (spk_2)", 25, "   "),
    ]
    assert len(pure.build_turns(noise)) == len(MEETING)


# --------------------------------------------------------------------------
# Which voices a pass will work on
# --------------------------------------------------------------------------


def test_a_voice_that_already_reads_as_a_person_is_not_unresolved() -> None:
    """A Virtual Participant names from the meeting roster, so there is nothing
    to infer even though the voice carries an engine voice id."""
    roster = [seg("Alice (spk_0)", 0, "hello"), seg("Bob (spk_0)", 5, "hi")]
    assert pure.unresolved_voices(pure.build_turns(roster), empty()) == []


def test_a_named_voice_is_not_revisited() -> None:
    identities = {**empty(), "identities": {SPK0: {"name": "Sarah Lee"}}}
    assert pure.unresolved_voices(turns(), identities) == [SPK1]


def test_a_voice_merged_into_another_is_not_named_separately() -> None:
    identities = {**empty(), "merges": {SPK1: SPK0}}
    assert pure.unresolved_voices(turns(), identities) == [SPK0]


def test_an_undiarized_channel_is_still_nameable() -> None:
    """With separation off the whole channel is one voice, and naming it is useful."""
    plain = [seg("Other Participant", 0, "hello there")]
    assert pure.unresolved_voices(pure.build_turns(plain), empty()) == ["CALLER#Other Participant"]


# --------------------------------------------------------------------------
# Skipping the model when there is nothing to find
# --------------------------------------------------------------------------


def test_a_transcript_with_no_naming_language_skips_the_model() -> None:
    """The miner runs repeatedly during a meeting; most passes see nothing that
    could name anyone, and those should not cost a model call."""
    chatter = [
        seg("Other Participant (spk_0)", 0, "the numbers moved three percent last quarter"),
        seg("Other Participant (spk_1)", 5, "we should look at the second chart"),
    ]
    assert pure.worth_a_model_call(pure.build_turns(chatter), empty()) is False


def test_an_introduction_is_worth_a_model_call() -> None:
    assert pure.worth_a_model_call(turns(), empty()) is True


def test_nothing_left_to_name_skips_the_model_even_on_demand() -> None:
    """Pressing the button on a fully named meeting should not call a model."""
    identities = {
        **empty(),
        "identities": {SPK0: {"name": "Sarah Lee"}, SPK1: {"name": "Priya Raman"}},
    }
    assert pure.worth_a_model_call(turns(), identities, force=True) is False


def test_on_demand_overrides_the_heuristic() -> None:
    """Somebody pressed the button, so answer them even if the shortcut says no."""
    chatter = [
        seg("Other Participant (spk_0)", 0, "the numbers moved three percent"),
        seg("Other Participant (spk_1)", 5, "look at the second chart"),
    ]
    built = pure.build_turns(chatter)
    assert pure.worth_a_model_call(built, empty(), force=True) is True


# --------------------------------------------------------------------------
# Attribution -- recomputed, never trusted
# --------------------------------------------------------------------------


def test_a_self_introduction_names_the_speaker_of_that_turn() -> None:
    key, evidence = pure.attribute(
        {"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"}, by_number()
    )
    assert key == SPK0
    assert evidence["turn"] == 1


def test_being_addressed_names_the_PREVIOUS_speaker_not_the_one_talking() -> None:
    """'That is a good point, Sarah' is said by spk_1 about spk_0."""
    key, _ = pure.attribute({"basis": pure.ADDRESSED_BY_NAME, "turn": 4}, by_number())
    assert key == SPK0


def test_a_name_used_mid_run_by_the_same_voice_is_dropped() -> None:
    """If the previous turn is the same voice, nobody was being addressed across a
    turn boundary and the inference has no basis."""
    same_voice = pure.build_turns(
        [
            seg("Other Participant (spk_0)", 0, "let me start"),
            seg("Other Participant (spk_0)", 5, "good point, Sarah"),
        ]
    )
    key, _ = pure.attribute(
        {"basis": pure.ADDRESSED_BY_NAME, "turn": 2}, {t["turn"]: t for t in same_voice}
    )
    assert key is None


def test_being_addressed_in_the_first_turn_is_dropped() -> None:
    key, _ = pure.attribute({"basis": pure.ADDRESSED_BY_NAME, "turn": 1}, by_number())
    assert key is None


def test_a_label_that_contradicts_the_cited_turn_is_dropped() -> None:
    """The model's label is a hint that must agree with the turn index, so a
    mislabelled proposal is dropped rather than applied to the wrong voice."""
    key, _ = pure.attribute(
        {"basis": pure.SELF_INTRODUCTION, "turn": 1, "label": SPK1}, by_number()
    )
    assert key is None


def test_a_turn_number_that_does_not_exist_is_dropped() -> None:
    for bad in (0, 99, -1, None, "two", 1.5):
        key, _ = pure.attribute({"basis": pure.SELF_INTRODUCTION, "turn": bad}, by_number())
        assert key is None, bad


def test_an_unrecognised_basis_is_dropped() -> None:
    """A model inventing its own evidence category names nobody."""
    for basis in ("third-party-mention", "guess", "role", None, ""):
        key, _ = pure.attribute({"basis": basis, "turn": 1}, by_number())
        assert key is None, basis


# --------------------------------------------------------------------------
# Name hygiene
# --------------------------------------------------------------------------


def test_placeholder_names_are_rejected() -> None:
    """Models reach for these when the transcript does not support a name, and an
    unnamed voice is better than a voice called 'Speaker 1'."""
    for bad in (
        "Unknown",
        "unknown speaker",
        "Speaker 1",
        "speaker_2",
        "Participant",
        "Other Participant",
        "spk_0",
        "N/A",
        "none",
        "",
        "   ",
    ):
        assert pure.clean_name(bad) is None, bad


def test_a_sentence_is_not_a_name() -> None:
    assert pure.clean_name("The person who introduced themselves as Sarah") is None
    assert pure.clean_name("x" * 80) is None


def test_real_names_survive_with_punctuation_trimmed() -> None:
    assert pure.clean_name("  Sarah Lee. ") == "Sarah Lee"
    assert pure.clean_name("Bob Strahan") == "Bob Strahan"
    assert pure.clean_name("Priya") == "Priya"
    assert pure.clean_name("Jean-Luc Picard") == "Jean-Luc Picard"
    assert pure.clean_name("O'Brien") == "O'Brien"


def test_a_non_string_name_is_rejected() -> None:
    for bad in (None, 42, [], {}):
        assert pure.clean_name(bad) is None


# --------------------------------------------------------------------------
# Parsing the model's answer
# --------------------------------------------------------------------------


def test_a_bare_array_is_parsed() -> None:
    assert pure.parse_model_response('[{"label": "a", "name": "Bob"}]') == [
        {"label": "a", "name": "Bob"}
    ]


def test_an_array_wrapped_in_prose_or_fences_is_parsed() -> None:
    """Insisting on a bare array would throw away otherwise good answers."""
    wrapped = 'Here is what I found:\n```json\n[{"name": "Bob"}]\n```\nHope that helps.'
    assert pure.parse_model_response(wrapped) == [{"name": "Bob"}]


def test_an_unparseable_answer_names_nobody() -> None:
    for bad in ("", None, "I could not determine any names.", "[{broken", "{}", "null"):
        assert pure.parse_model_response(bad) == []


def test_non_object_entries_are_discarded() -> None:
    assert pure.parse_model_response('["Bob", 3, {"name": "Sarah"}]') == [{"name": "Sarah"}]


# --------------------------------------------------------------------------
# Applying proposals -- the policy
# --------------------------------------------------------------------------


def test_a_self_introduction_is_applied_as_confirmed() -> None:
    """The speaker said their own name; that is as good as evidence gets."""
    updated, applied = pure.apply_proposals(
        empty(),
        [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee", "quote": "I'm Sarah Lee"}],
        turns(),
        NOW,
    )
    entry = updated["identities"][SPK0]
    assert entry["name"] == "Sarah Lee"
    assert entry["status"] == "confirmed"
    assert entry["source"] == pure.SELF_INTRODUCTION
    assert entry["evidence"]["quote"] == "I'm Sarah Lee"
    assert entry["evidence"]["turn"] == 1
    assert applied == [
        {"key": SPK0, "name": "Sarah Lee", "status": "confirmed", "basis": pure.SELF_INTRODUCTION}
    ]


def test_being_addressed_is_only_ever_proposed() -> None:
    """This inference rides on the turn boundaries being right, and in live
    testing they were not always, so a human confirms it."""
    updated, _ = pure.apply_proposals(
        empty(), [{"basis": pure.ADDRESSED_BY_NAME, "turn": 4, "name": "Sarah"}], turns(), NOW
    )
    assert updated["identities"][SPK0]["status"] == "proposed"


def test_a_name_a_human_typed_is_never_overwritten() -> None:
    """Human judgement outranks anything inferred -- that is the whole point of
    the confirmed/proposed split."""
    identities = {
        **empty(),
        "identities": {SPK0: {"name": "Sarah Lee", "status": "confirmed", "source": "manual"}},
    }
    updated, applied = pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Somebody Else"}],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"
    assert applied == []


def test_a_locked_name_is_never_overwritten() -> None:
    identities = {
        **empty(),
        "identities": {SPK0: {"name": "Sarah Lee", "status": "proposed", "locked": True}},
    }
    updated, applied = pure.apply_proposals(
        identities, [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Wrong"}], turns(), NOW
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"
    assert applied == []


def test_a_self_introduction_may_upgrade_an_earlier_proposal() -> None:
    """A guess from being addressed should yield to the person saying their own
    name."""
    identities = {
        **empty(),
        "identities": {
            SPK0: {"name": "Sara", "status": "proposed", "source": pure.ADDRESSED_BY_NAME}
        },
    }
    updated, _ = pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0] == {
        **updated["identities"][SPK0],
        "name": "Sarah Lee",
        "status": "confirmed",
    }


def test_a_weaker_proposal_does_not_overwrite_a_confirmed_name() -> None:
    identities = {
        **empty(),
        "identities": {
            SPK0: {"name": "Sarah Lee", "status": "confirmed", "source": pure.SELF_INTRODUCTION}
        },
    }
    updated, applied = pure.apply_proposals(
        identities, [{"basis": pure.ADDRESSED_BY_NAME, "turn": 4, "name": "Sara"}], turns(), NOW
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"
    assert applied == []


def test_one_name_cannot_be_given_to_two_voices_on_a_channel() -> None:
    """Two rows with the same name read as one person in two places, which then
    confuses every summary."""
    updated, applied = pure.apply_proposals(
        empty(),
        [
            {"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"},
            {"basis": pure.SELF_INTRODUCTION, "turn": 2, "name": "Sarah Lee"},
        ],
        turns(),
        NOW,
    )
    assert len(applied) == 1
    assert SPK1 not in updated["identities"]


def test_a_self_introduction_wins_the_uniqueness_check_over_a_weaker_claim() -> None:
    """Order of proposals in the response must not decide which voice keeps a
    contested name."""
    updated, _ = pure.apply_proposals(
        empty(),
        [
            {"basis": pure.ADDRESSED_BY_NAME, "turn": 4, "name": "Sarah Lee"},
            {"basis": pure.SELF_INTRODUCTION, "turn": 2, "name": "Sarah Lee"},
        ],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK1]["name"] == "Sarah Lee"
    assert updated["identities"][SPK1]["status"] == "confirmed"
    assert SPK0 not in updated["identities"]


def test_the_same_name_on_different_channels_is_allowed() -> None:
    """One person can legitimately be heard on both the microphone and the
    meeting audio."""
    two_channels = pure.build_turns(
        [
            seg("Other Participant (spk_0)", 0, "Hi, I'm Sarah Lee"),
            seg("Other Participant (spk_0)", 5, "Hi, I'm Sarah Lee", channel="AGENT"),
        ]
    )
    updated, applied = pure.apply_proposals(
        empty(),
        [
            {"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"},
            {"basis": pure.SELF_INTRODUCTION, "turn": 2, "name": "Sarah Lee"},
        ],
        two_channels,
        NOW,
    )
    assert len(applied) == 2


def test_a_voice_that_already_reads_as_a_person_is_left_alone() -> None:
    roster = pure.build_turns([seg("Alice (spk_0)", 0, "Hi, I'm actually Bob")])
    updated, applied = pure.apply_proposals(
        empty(), [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Bob"}], roster, NOW
    )
    assert applied == []
    assert updated["identities"] == {}


def test_the_stored_map_is_not_mutated() -> None:
    identities = {**empty(), "candidates": ["Sarah Lee"]}
    before = repr(identities)
    pure.apply_proposals(
        identities, [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"}], turns(), NOW
    )
    assert repr(identities) == before


def test_merges_overrides_and_candidates_survive_a_pass() -> None:
    identities = {
        **empty(),
        "candidates": ["Sarah Lee", "Priya Raman"],
        "merges": {SPK2: SPK1},
        "segmentOverrides": {"s15": "Priya Raman"},
    }
    updated, _ = pure.apply_proposals(
        identities, [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"}], turns(), NOW
    )
    assert updated["candidates"] == ["Sarah Lee", "Priya Raman"]
    assert updated["merges"] == {SPK2: SPK1}
    assert updated["segmentOverrides"] == {"s15": "Priya Raman"}


def test_an_empty_response_changes_nothing() -> None:
    updated, applied = pure.apply_proposals(empty(), [], turns(), NOW)
    assert applied == []
    assert updated["identities"] == {}


def test_a_hostile_response_changes_nothing() -> None:
    """Nothing a model returns should be able to name a voice the transcript does
    not support, whatever shape the answer takes."""
    hostile = [
        {"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Unknown"},
        {"basis": pure.SELF_INTRODUCTION, "turn": 999, "name": "Ghost"},
        {"basis": pure.ADDRESSED_BY_NAME, "turn": 1, "name": "Nobody"},
        {"basis": "vibes", "turn": 2, "name": "Guessed"},
        {"basis": pure.SELF_INTRODUCTION, "turn": 1, "label": SPK1, "name": "Mislabelled"},
        {"name": "No basis at all"},
        {},
    ]
    updated, applied = pure.apply_proposals(empty(), hostile, turns(), NOW)
    assert applied == []
    assert updated["identities"] == {}


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------


def test_the_prompt_carries_the_numbered_transcript_and_the_open_labels() -> None:
    prompt = pure.build_prompt(turns(), [SPK0, SPK1], [])
    assert "[1] CALLER#Other Participant (spk_0): Hi everyone, I'm Sarah Lee" in prompt
    assert f"- {SPK0}" in prompt
    assert pure.SELF_INTRODUCTION in prompt
    assert pure.ADDRESSED_BY_NAME in prompt


def test_the_pasted_attendee_list_is_offered_but_not_imposed() -> None:
    """A roster makes the model far more reliable, but somebody not on it can
    still introduce themselves -- a meeting often has a walk-in."""
    prompt = pure.build_prompt(turns(), [SPK0], ["Sarah Lee", "Priya Raman"])
    assert "- Sarah Lee" in prompt
    assert "Do not feel limited to this list" in prompt


def test_no_attendee_list_leaves_that_section_out_entirely() -> None:
    """The no-roster meeting is the normal case and must not get an empty header."""
    prompt = pure.build_prompt(turns(), [SPK0], [])
    assert "expected to be in this meeting" not in prompt


def test_the_prompt_tells_the_model_to_prefer_silence_to_guessing() -> None:
    prompt = pure.build_prompt(turns(), [SPK0], [])
    assert "a wrong name is worse than no name" in prompt


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------
# Reading the stored map
# --------------------------------------------------------------------------


def test_a_meeting_with_no_stored_map_reads_as_empty() -> None:
    for raw in (None, "", "{}"):
        assert pure.parse_identities(raw)["identities"] == {}


def test_a_corrupt_stored_map_reads_as_empty_rather_than_raising() -> None:
    """A pass that could not read the existing names would re-propose names a
    human has already settled, so this has to degrade rather than fail."""
    for raw in ("{not json", '"a string"', "[1,2]", "null"):
        parsed = pure.parse_identities(raw)
        assert parsed["identities"] == {}
        assert parsed["merges"] == {}
        assert parsed["candidates"] == []


def test_the_stored_map_round_trips() -> None:
    stored = {
        "version": 1,
        "candidates": ["Sarah Lee"],
        "identities": {SPK0: {"name": "Sarah Lee", "status": "confirmed"}},
        "merges": {SPK2: SPK1},
        "segmentOverrides": {"s5": "Priya"},
        "rejected": {SPK1: ["Wrong Person"]},
    }
    assert pure.parse_identities(json.dumps(stored)) == stored


def test_sections_of_the_wrong_type_are_replaced_with_empty_ones() -> None:
    parsed = pure.parse_identities('{"identities": "nope", "candidates": {"a": 1}}')
    assert parsed["identities"] == {}
    assert parsed["candidates"] == []


def test_a_name_the_user_rejected_is_not_proposed_again() -> None:
    """Otherwise an automatic pass would keep putting back a name a person has
    already said is wrong, every time it runs."""
    identities = {**empty(), "rejected": {SPK0: ["Sarah Lee"]}}
    updated, applied = pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert applied == []
    assert SPK0 not in updated["identities"]


def test_a_rejection_does_not_block_a_different_name_for_that_voice() -> None:
    identities = {**empty(), "rejected": {SPK0: ["Sara Leigh"]}}
    updated, applied = pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"
    assert len(applied) == 1


def test_rejections_survive_a_pass() -> None:
    identities = {**empty(), "rejected": {SPK1: ["Wrong Person"]}}
    updated, _ = pure.apply_proposals(
        identities, [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"}], turns(), NOW
    )
    assert updated["rejected"] == {SPK1: ["Wrong Person"]}
