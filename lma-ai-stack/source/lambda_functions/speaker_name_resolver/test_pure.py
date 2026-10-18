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
    seg("Other Participant (spk_0)", 20, "Thanks Sarah Lee, that is settled then"),
    seg("Other Participant (spk_1)", 25, "Just to be clear, I'm Sarah Lee as well"),
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
    assert numbered == [(1, SPK0), (2, SPK1), (3, SPK0), (4, SPK1), (5, SPK0), (6, SPK1)]


def test_turn_order_does_not_depend_on_the_order_segments_were_stored() -> None:
    shuffled = [MEETING[2], MEETING[0], MEETING[3], MEETING[1]]
    assert [t["turn"] for t in pure.build_turns(shuffled)] == [1, 2, 3, 4]
    assert [t["key"] for t in pure.build_turns(shuffled)] == [SPK0, SPK1, SPK0, SPK1]


def test_segments_sharing_a_start_time_are_ordered_deterministically() -> None:
    """Two passes over the same data must number turns identically, or the model's
    cited turn number would point somewhere else on verification."""
    tied = [
        seg("A (spk_0)", 1, "first", segment_id="b"),
        seg("A (spk_1)", 1, "second", segment_id="a"),
    ]
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
        {"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"},
        by_number(),
    )
    assert key == SPK0
    assert evidence["turn"] == 1


def test_being_addressed_names_the_PREVIOUS_speaker_not_the_one_talking() -> None:
    """'That is a good point, Sarah' is said by spk_1 about spk_0."""
    key, _ = pure.attribute(
        {"basis": pure.ADDRESSED_BY_NAME, "label": SPK0, "turn": 4}, by_number()
    )
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
        [
            {
                "basis": pure.SELF_INTRODUCTION,
                "label": SPK0,
                "turn": 1,
                "name": "Sarah Lee",
                "quote": "I'm Sarah Lee",
            }
        ],
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
        empty(),
        [{"basis": pure.ADDRESSED_BY_NAME, "label": SPK0, "turn": 4, "name": "Sarah"}],
        turns(),
        NOW,
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
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
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
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
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
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
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
        identities,
        [{"basis": pure.ADDRESSED_BY_NAME, "label": SPK0, "turn": 4, "name": "Sarah"}],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"
    assert applied == []


def test_one_name_cannot_be_given_to_two_voices_on_a_channel() -> None:
    """Two rows with the same name read as one person in two places, which then
    confuses every summary."""
    updated, applied = pure.apply_proposals(
        empty(),
        [
            {"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"},
            {"basis": pure.ADDRESSED_BY_NAME, "label": SPK1, "turn": 5, "name": "Sarah Lee"},
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
            {"basis": pure.ADDRESSED_BY_NAME, "label": SPK0, "turn": 4, "name": "Sarah Lee"},
            {"basis": pure.SELF_INTRODUCTION, "label": SPK1, "turn": 6, "name": "Sarah Lee"},
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
            {"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"},
            {
                "basis": pure.SELF_INTRODUCTION,
                "label": "AGENT#Other Participant (spk_0)",
                "turn": 2,
                "name": "Sarah Lee",
            },
        ],
        two_channels,
        NOW,
    )
    assert len(applied) == 2


def test_a_voice_that_already_reads_as_a_person_is_left_alone() -> None:
    roster = pure.build_turns([seg("Alice (spk_0)", 0, "Hi, I'm actually Bob")])
    updated, applied = pure.apply_proposals(
        empty(),
        [
            {
                "basis": pure.SELF_INTRODUCTION,
                "label": "CALLER#Alice (spk_0)",
                "turn": 1,
                "name": "Bob",
            }
        ],
        roster,
        NOW,
    )
    assert applied == []
    assert updated["identities"] == {}


def test_the_stored_map_is_not_mutated() -> None:
    identities = {**empty(), "candidates": ["Sarah Lee"]}
    before = repr(identities)
    pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
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
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
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
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert applied == []
    assert SPK0 not in updated["identities"]


def test_a_rejection_does_not_block_a_different_name_for_that_voice() -> None:
    identities = {**empty(), "rejected": {SPK0: ["Sara Leigh"]}}
    updated, applied = pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"
    assert len(applied) == 1


def test_rejections_survive_a_pass() -> None:
    identities = {**empty(), "rejected": {SPK1: ["Wrong Person"]}}
    updated, _ = pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert updated["rejected"] == {SPK1: ["Wrong Person"]}


# --------------------------------------------------------------------------
# What the verification actually guarantees
#
# The model chooses the turn and the turn chooses the voice, so two further
# cross-checks are what stop the model choosing the voice on its own: a
# self-introduction must name the label it claims, and the name must appear in
# the words of the turn cited. Both cases below defeated an earlier version.
# --------------------------------------------------------------------------


def test_a_self_introduction_with_no_label_names_nobody() -> None:
    """Without a label there is nothing to cross-check the cited turn against, so
    the model alone would be choosing the voice."""
    updated, applied = pure.apply_proposals(
        empty(),
        [{"basis": pure.SELF_INTRODUCTION, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert applied == []
    assert updated["identities"] == {}


def test_being_addressed_with_no_label_names_nobody() -> None:
    updated, applied = pure.apply_proposals(
        empty(), [{"basis": pure.ADDRESSED_BY_NAME, "turn": 4, "name": "Sarah"}], turns(), NOW
    )
    assert applied == []
    assert updated["identities"] == {}


def test_a_name_that_is_not_in_the_cited_turn_is_dropped() -> None:
    """A self-consistent citation for a name nobody said. This is the shape of a
    real boundary error: the diarizer put "and I'm Priya Raman" inside another
    speaker's segment, so a proposal naming that segment's voice Priya looks
    internally consistent and is still wrong.
    """
    updated, applied = pure.apply_proposals(
        empty(),
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Priya Raman"}],
        turns(),
        NOW,
    )
    assert applied == []
    assert SPK0 not in updated["identities"]


def test_a_name_the_transcript_misheard_slightly_is_still_accepted() -> None:
    """The transcript is speech: "Sarah Lee" is supported by a turn that says
    "Sara Lee", and refusing that would reject most real introductions."""
    misheard = pure.build_turns([seg("Other Participant (spk_0)", 0, "Hi, I'm Sara Lee")])
    updated, applied = pure.apply_proposals(
        empty(),
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        misheard,
        NOW,
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"
    assert len(applied) == 1


def test_a_first_name_alone_is_enough_support() -> None:
    assert pure.name_appears_in("Sarah Lee", "thanks Sarah, go on") is True


def test_a_name_absent_from_the_words_is_not_supported() -> None:
    assert pure.name_appears_in("Priya Raman", "I own the data ingestion side") is False
    assert pure.name_appears_in("", "anything") is False
    assert pure.name_appears_in("Bob", None) is False


def test_an_initial_is_not_treated_as_support() -> None:
    """A single letter appears in almost any sentence."""
    assert pure.name_appears_in("A", "that is a good point") is False


# --------------------------------------------------------------------------
# Merged voices
# --------------------------------------------------------------------------


def test_an_introduction_on_a_merged_away_voice_names_the_target() -> None:
    """The usual reason to merge is that the engine split one person, so the
    introduction very often sits on the half that was merged away. Dropping it
    threw away the best evidence in the meeting."""
    identities = {**empty(), "merges": {SPK1: SPK0}}
    updated, applied = pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK1, "turn": 6, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"
    assert SPK1 not in updated["identities"]
    assert applied == [
        {"key": SPK0, "name": "Sarah Lee", "status": "confirmed", "basis": pure.SELF_INTRODUCTION}
    ]


def test_a_merge_cycle_does_not_hang_the_pass() -> None:
    identities = {**empty(), "merges": {SPK0: SPK1, SPK1: SPK0}}
    pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )


# --------------------------------------------------------------------------
# A stored map is arbitrary JSON a client wrote
# --------------------------------------------------------------------------


def test_a_stored_entry_whose_name_is_not_a_string_does_not_break_the_pass() -> None:
    """updateSpeakerIdentities accepts any JSON string, so a client can write
    this -- and it must not permanently disable naming for that meeting."""
    identities = {**empty(), "identities": {SPK1: {"name": 123}}}
    updated, applied = pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"
    assert len(applied) == 1


def test_a_stored_entry_that_is_not_a_dict_does_not_break_the_pass() -> None:
    identities = {**empty(), "identities": {SPK1: "just a string"}}
    pure.unresolved_voices(turns(), identities)
    updated, _ = pure.apply_proposals(
        identities,
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah Lee"}],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0]["name"] == "Sarah Lee"


# --------------------------------------------------------------------------
# The evidence shown to a human
# --------------------------------------------------------------------------


def test_a_quote_the_turn_does_not_contain_is_replaced_with_the_turn() -> None:
    """The quote is shown as the reason to accept a name, so a sentence nobody
    said would undermine the one decision the human is asked to make."""
    updated, _ = pure.apply_proposals(
        empty(),
        [
            {
                "basis": pure.SELF_INTRODUCTION,
                "label": SPK0,
                "turn": 1,
                "name": "Sarah Lee",
                "quote": "my name is Dave and I am the CEO",
            }
        ],
        turns(),
        NOW,
    )
    quote = updated["identities"][SPK0]["evidence"]["quote"]
    assert quote == turns()[0]["text"]


def test_a_real_quote_from_the_turn_is_kept() -> None:
    updated, _ = pure.apply_proposals(
        empty(),
        [
            {
                "basis": pure.SELF_INTRODUCTION,
                "label": SPK0,
                "turn": 1,
                "name": "Sarah Lee",
                "quote": "I'm Sarah Lee",
            }
        ],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0]["evidence"]["quote"] == "I'm Sarah Lee"


# --------------------------------------------------------------------------
# Name hygiene, after the rewrite that removed a pathological regex
# --------------------------------------------------------------------------


def test_classifying_a_name_is_fast_whatever_it_contains() -> None:
    """An earlier form of this check backtracked exponentially: a 28-character
    run of digits took 96 seconds, enough to exhaust the function's whole
    timeout, and the value came from a model reading untrusted text."""
    import time

    for probe in ("1" * 59, ("1 " * 40)[:59], ("speaker_" * 10)[:59], ("1-" * 40)[:59], "a" * 59):
        started = time.monotonic()
        pure.clean_name(probe)
        assert time.monotonic() - started < 0.05, probe


def test_a_name_in_any_script_is_accepted() -> None:
    """Testing for [A-Za-z] silently switched naming off for Chinese, Cyrillic,
    Arabic, Greek and Indic meetings."""
    for name in ("张伟", "Иван Петров", "أحمد", "Ανδρέας", "नरेन्द्र", "Renée", "Müller"):
        assert pure.clean_name(name) == name


def test_a_short_real_name_is_accepted() -> None:
    """'An' is a common Vietnamese and Chinese given name, and was rejected as a
    generic word."""
    assert pure.clean_name("An") == "An"
    assert pure.clean_name("Al") == "Al"


def test_generic_names_are_still_rejected_in_combination() -> None:
    for bad in (
        "Speaker 1",
        "speaker_2",
        "Person 2",
        "spk_0",
        "unidentified male",
        "the other person",
        "Unknown Speaker",
        "N/A",
        "TBD",
        "Anonymous",
    ):
        assert pure.clean_name(bad) is None, bad


# --------------------------------------------------------------------------
# Writing without clobbering
#
# A pass reads the map, spends seconds on a model call, then writes. The write
# replaces the whole blob with no ordering guard -- deliberately, so a human
# rename always wins -- so only the keys this pass changed may be applied, onto
# whatever is stored by then.
# --------------------------------------------------------------------------


def test_a_rename_typed_during_the_pass_is_not_discarded() -> None:
    latest = {
        **empty(),
        "identities": {SPK1: {"name": "Alice", "status": "confirmed", "source": "manual"}},
    }
    merged, changed = pure.merge_applied_onto(
        latest, {SPK0: {"name": "Sarah Lee", "status": "confirmed"}}
    )
    assert merged["identities"][SPK1]["name"] == "Alice"
    assert merged["identities"][SPK0]["name"] == "Sarah Lee"
    assert changed is True


def test_a_rename_of_the_same_voice_during_the_pass_wins() -> None:
    """The person watching is better informed than the pass that started before
    they typed."""
    latest = {
        **empty(),
        "identities": {SPK0: {"name": "Alice", "status": "confirmed", "source": "manual"}},
    }
    merged, changed = pure.merge_applied_onto(
        latest, {SPK0: {"name": "Sarah Lee", "status": "confirmed"}}
    )
    assert merged["identities"][SPK0]["name"] == "Alice"
    assert changed is False


def test_a_suggestion_does_not_overwrite_a_confirmed_name_that_arrived_meanwhile() -> None:
    latest = {**empty(), "identities": {SPK0: {"name": "Alice", "status": "confirmed"}}}
    merged, changed = pure.merge_applied_onto(
        latest, {SPK0: {"name": "Sarah", "status": "proposed"}}
    )
    assert merged["identities"][SPK0]["name"] == "Alice"
    assert changed is False


def test_a_name_rejected_during_the_pass_is_not_written() -> None:
    latest = {**empty(), "rejected": {SPK0: ["Sarah Lee"]}}
    merged, changed = pure.merge_applied_onto(
        latest, {SPK0: {"name": "Sarah Lee", "status": "confirmed"}}
    )
    assert SPK0 not in merged["identities"]
    assert changed is False


def test_merging_preserves_everything_else_in_the_stored_map() -> None:
    latest = {
        **empty(),
        "candidates": ["Priya Raman"],
        "merges": {SPK2: SPK1},
        "segmentOverrides": {"s5": "Bob"},
        "rejected": {SPK1: ["Wrong"]},
    }
    merged, _ = pure.merge_applied_onto(
        latest, {SPK0: {"name": "Sarah Lee", "status": "confirmed"}}
    )
    assert merged["candidates"] == ["Priya Raman"]
    assert merged["merges"] == {SPK2: SPK1}
    assert merged["segmentOverrides"] == {"s5": "Bob"}
    assert merged["rejected"] == {SPK1: ["Wrong"]}


def test_merging_nothing_reports_no_change() -> None:
    merged, changed = pure.merge_applied_onto(empty(), {})
    assert changed is False
    assert merged["identities"] == {}


# --------------------------------------------------------------------------
# Trimming a long transcript
# --------------------------------------------------------------------------


def test_a_short_transcript_is_passed_whole() -> None:
    assert len(pure.select_turns(turns(), 160)) == len(turns())


def test_a_long_transcript_keeps_the_opening_and_the_most_recent_turns() -> None:
    """Head only meant that past the limit every pass saw identical input, so the
    end-of-meeting pass was no better than the first and a late introduction
    could never be found."""
    long_meeting = pure.build_turns(
        [seg("Other Participant (spk_0)", i * 5, f"turn {i}") for i in range(300)]
    )
    selected = pure.select_turns(long_meeting, 30)
    assert len(selected) == 30
    assert selected[0]["turn"] == 1
    assert selected[-1]["turn"] == 300


def test_trimming_preserves_turn_numbers_rather_than_renumbering() -> None:
    """They are what attribution is recomputed from, and the prompt shows them."""
    long_meeting = pure.build_turns(
        [seg("Other Participant (spk_0)", i * 5, f"turn {i}") for i in range(300)]
    )
    selected = pure.select_turns(long_meeting, 30)
    by_number = {t["turn"]: t for t in selected}
    for turn in selected:
        assert by_number[turn["turn"]] is turn
    assert pure.format_transcript(selected).startswith("[1] ")


def test_a_late_introduction_can_still_be_attributed() -> None:
    """The point of keeping the tail."""
    rows = [seg("Other Participant (spk_0)", i * 5, f"turn {i}") for i in range(200)]
    rows.append(seg("Other Participant (spk_1)", 2000, "By the way, I'm Priya Raman"))
    selected = pure.select_turns(pure.build_turns(rows), 30)
    updated, applied = pure.apply_proposals(
        empty(),
        [
            {
                "basis": pure.SELF_INTRODUCTION,
                "label": SPK1,
                "turn": selected[-1]["turn"],
                "name": "Priya Raman",
            }
        ],
        selected,
        NOW,
    )
    assert updated["identities"][SPK1]["name"] == "Priya Raman"
    assert len(applied) == 1


def test_trimming_is_disabled_by_a_non_positive_limit() -> None:
    assert len(pure.select_turns(turns(), 0)) == len(turns())


# --------------------------------------------------------------------------
# The growth check must not saturate
#
# The trimmed list the model reads caps at MAX_TURNS, so comparing it against a
# stamp taken from it became permanently false past the cap -- which silenced
# naming for the rest of the meeting, the end-of-meeting pass included.
# --------------------------------------------------------------------------


def test_growth_is_measured_against_the_whole_transcript_not_the_trimmed_list() -> None:
    stamped_at_the_cap = {**empty(), "lastPass": {"at": NOW, "turns": 160}}
    assert pure.transcript_has_grown(stamped_at_the_cap, 300) is True
    assert pure.transcript_has_grown(stamped_at_the_cap, 160) is False
    assert pure.transcript_has_grown(stamped_at_the_cap, 161) is True


def test_a_first_pass_always_counts_as_grown() -> None:
    assert pure.transcript_has_grown(empty(), 1) is True
    assert pure.transcript_has_grown({**empty(), "lastPass": {"at": NOW}}, 1) is True


def test_a_tiny_turn_limit_does_not_return_more_than_it_was_asked_for() -> None:
    """turns[-0:] is the whole list, so tail == 0 had to be special-cased."""
    long_meeting = pure.build_turns(
        [seg("Other Participant (spk_0)", i * 5, f"turn {i}") for i in range(20)]
    )
    for limit in (1, 2, 3):
        selected = pure.select_turns(long_meeting, limit)
        assert len(selected) <= limit, limit
        assert len({t["turn"] for t in selected}) == len(selected), limit


# --------------------------------------------------------------------------
# Supporting a name, after substring matching proved far too generous
# --------------------------------------------------------------------------


def test_a_short_name_is_not_supported_by_a_longer_word_containing_it() -> None:
    """Measured against the previous version: "An" was supported by the word
    "and", "Al" by "also", "Mark" by "market", "Alan" by "balance"."""
    assert pure.name_appears_in("An", "that is a good point and I agree") is False
    assert pure.name_appears_in("Al", "we should also look at this") is False
    assert pure.name_appears_in("Mark", "the market moved three percent") is False
    assert pure.name_appears_in("Alan", "the balance is fine") is False
    assert pure.name_appears_in("Li", "I like that idea") is False


def test_a_name_spoken_as_its_own_word_is_supported() -> None:
    assert pure.name_appears_in("An", "hi, I'm An, I lead the team") is True
    assert pure.name_appears_in("Mark", "thanks Mark, go ahead") is True
    assert pure.name_appears_in("Sarah Lee", "I'm Sarah Lee from platform") is True


def test_a_transcription_slip_is_still_allowed_on_a_longer_name() -> None:
    assert pure.name_appears_in("Sarah Lee", "hi, I'm Sara Lee") is True
    assert pure.name_appears_in("Priya", "this is Priyah speaking") is True


# --------------------------------------------------------------------------
# A self-introduction has to look like one
# --------------------------------------------------------------------------


def test_a_turn_that_is_plainly_being_addressed_cannot_be_a_self_introduction() -> None:
    """A mislabelled basis would otherwise apply a CONFIRMED name off the back of
    "That is a great point, Sarah" -- the exact mistake the basis prevents."""
    addressed = pure.build_turns(
        [seg("Other Participant (spk_0)", 0, "That is a great point, Sarah")]
    )
    updated, applied = pure.apply_proposals(
        empty(),
        [{"basis": pure.SELF_INTRODUCTION, "label": SPK0, "turn": 1, "name": "Sarah"}],
        addressed,
        NOW,
    )
    assert applied == []
    assert updated["identities"] == {}


def test_real_introductions_are_recognised() -> None:
    for text in (
        "Hi everyone, I'm Sarah Lee",
        "my name is Sarah Lee",
        "Sarah Lee here",
        "this is Sarah Lee",
        "Sarah Lee speaking",
        "I'll go first, I am Sarah Lee",
    ):
        assert pure.looks_like_a_self_introduction(text) is True, text


def test_being_thanked_or_credited_is_not_an_introduction() -> None:
    for text in (
        "thanks Sarah",
        "good point, Sarah",
        "Sarah, do you want to start",
        "Sarah said that yesterday",
    ):
        assert pure.looks_like_a_self_introduction(text) is False, text


def test_being_addressed_does_not_require_an_introduction_phrase() -> None:
    """The phrase check applies only to self-introductions; the addressed-by-name
    basis is about the PREVIOUS turn and is only ever a suggestion anyway."""
    updated, applied = pure.apply_proposals(
        empty(),
        [{"basis": pure.ADDRESSED_BY_NAME, "label": SPK0, "turn": 4, "name": "Sarah"}],
        turns(),
        NOW,
    )
    assert updated["identities"][SPK0]["status"] == "proposed"
    assert len(applied) == 1


def test_a_placeholder_name_with_a_trailing_dot_is_still_rejected() -> None:
    assert pure.clean_name("n.a.") is None
    assert pure.clean_name("N.A.") is None
