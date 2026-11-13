#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the consent evidence verifier's judgement.

The model's answer is untrusted text: whatever it says, the stored shape is
fixed, an answer with no JSON claims nothing, and an objection is reported
whatever the confidence. The verdict for the each-participant rule needs every
listed participant's consent to be visible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))

import pure  # noqa: E402

PARTICIPANTS = ["Ana Silva", "Bo Chen"]


def answer(**overrides):
    base = {
        "noticeVisible": True,
        "noticeMatches": True,
        "replies": [
            {"name": "Ana Silva", "text": "I consent", "decision": "consent"},
            {"name": "bo chen", "text": "I agree", "decision": "consent"},
        ],
        "anyObjection": False,
        "confidence": 0.9,
        "summary": "The notice and two replies are visible.",
    }
    base.update(overrides)
    import json

    return json.dumps(base)


def test_a_fenced_answer_is_read() -> None:
    parsed = pure.parse_answer("Here you go:\n```json\n" + answer() + "\n```")
    assert parsed["Readable"] and parsed["NoticeVisible"]
    assert [r["Decision"] for r in parsed["Replies"]] == ["consent", "consent"]


def test_an_answer_without_json_claims_nothing() -> None:
    parsed = pure.parse_answer("I cannot see the image clearly.")
    assert parsed == {
        "Readable": False,
        "NoticeVisible": False,
        "NoticeMatches": False,
        "Replies": [],
        "AnyObjection": False,
        "Confidence": 0.0,
        "Summary": None,
    }
    assert pure.verdict(parsed, "notify", None, [])["Verdict"] == "inconclusive"


def test_values_of_the_wrong_kind_are_normalised() -> None:
    parsed = pure.parse_answer(
        answer(
            confidence="very", noticeVisible="yes", replies=[{"name": 3, "decision": "maybe"}, 7]
        )
    )
    assert parsed["Confidence"] == 0.0
    assert parsed["NoticeVisible"] is True
    assert parsed["Replies"] == [{"Name": None, "Text": None, "Decision": "unclear"}]
    assert pure.parse_answer(answer(confidence=7))["Confidence"] == 1.0


@pytest.mark.parametrize(
    "overrides,mode,rule,expected",
    [
        ({}, "opt_in", "each_participant", "supports_consent"),
        ({}, "notify", None, "supports_consent"),
        ({"anyObjection": True}, "opt_in", "owner_attests", "objection_found"),
        (
            {"replies": [{"name": "Bo Chen", "text": "I object", "decision": "objection"}]},
            "notify",
            None,
            "objection_found",
        ),
        ({"anyObjection": True, "confidence": 0.1}, "notify", None, "objection_found"),
        ({"confidence": 0.3}, "notify", None, "inconclusive"),
        ({"noticeVisible": False}, "notify", None, "inconclusive"),
        (
            {"replies": [{"name": "Ana Silva", "text": "I consent", "decision": "consent"}]},
            "opt_in",
            "each_participant",
            "inconclusive",
        ),
        (
            {"replies": [{"name": "Ana Silva", "text": "I consent", "decision": "consent"}]},
            "opt_in",
            "owner_attests",
            "supports_consent",
        ),
    ],
)
def test_the_verdict(overrides, mode, rule, expected) -> None:
    parsed = pure.parse_answer(answer(**overrides))
    assert pure.verdict(parsed, mode, rule, PARTICIPANTS)["Verdict"] == expected


def test_the_participants_without_visible_consent_are_named() -> None:
    parsed = pure.parse_answer(
        answer(replies=[{"name": "ANA  SILVA", "text": "I consent", "decision": "consent"}])
    )
    assert pure.verdict(parsed, "opt_in", "each_participant", PARTICIPANTS)[
        "MissingParticipants"
    ] == ["Bo Chen"]


def test_the_missing_participants_are_listed_only_for_each_participant() -> None:
    parsed = pure.parse_answer(answer(replies=[]))
    for mode, rule in (("notify", None), ("opt_in", "owner_attests")):
        assert pure.verdict(parsed, mode, rule, PARTICIPANTS)["MissingParticipants"] == []


def test_only_the_participants_who_consented_in_the_chat_are_looked_for() -> None:
    # Ana consented in the chat and is visible; Bo consented aloud, so the chat
    # cannot show it: the image supports consent and Bo is listed as not expected.
    parsed = pure.parse_answer(
        answer(replies=[{"name": "Ana Silva", "text": "I consent", "decision": "consent"}])
    )
    judged = pure.verdict(parsed, "opt_in", "each_participant", PARTICIPANTS, ["bo chen"])
    assert judged["Verdict"] == "supports_consent"
    assert judged["MissingParticipants"] == []
    assert judged["ConsentedOutsideChat"] == ["Bo Chen"]
    # Ana's chat consent is still looked for.
    nothing = pure.parse_answer(answer(replies=[]))
    judged = pure.verdict(nothing, "opt_in", "each_participant", PARTICIPANTS, ["Bo Chen"])
    assert judged["Verdict"] == "inconclusive"
    assert judged["MissingParticipants"] == ["Ana Silva"]


def test_everyone_consenting_aloud_needs_only_the_notice_and_no_objection() -> None:
    parsed = pure.parse_answer(answer(replies=[]))
    judged = pure.verdict(parsed, "opt_in", "each_participant", PARTICIPANTS, PARTICIPANTS)
    assert judged["Verdict"] == "supports_consent"
    assert judged["ConsentedOutsideChat"] == PARTICIPANTS
    objected = pure.parse_answer(answer(anyObjection=True, replies=[]))
    assert (
        pure.verdict(objected, "opt_in", "each_participant", PARTICIPANTS, PARTICIPANTS)["Verdict"]
        == "objection_found"
    )
    hidden = pure.parse_answer(answer(noticeVisible=False, replies=[]))
    assert (
        pure.verdict(hidden, "opt_in", "each_participant", PARTICIPANTS, PARTICIPANTS)["Verdict"]
        == "inconclusive"
    )


def test_consent_outside_the_chat_is_listed_only_for_each_participant() -> None:
    parsed = pure.parse_answer(answer(replies=[]))
    for mode, rule in (("notify", None), ("opt_in", "owner_attests")):
        assert (
            pure.verdict(parsed, mode, rule, PARTICIPANTS, ["Bo Chen"])["ConsentedOutsideChat"]
            == []
        )


def test_the_prompt_names_the_participants_who_consented_outside_the_chat() -> None:
    prompt = pure.build_prompt(
        "Notice", "opt_in", ["Ana", "Bo"], ["I consent"], ["I object"], [], ["Bo"]
    )
    assert "consented aloud or in LMA, not in the chat" in prompt
    assert prompt.count('"Bo"') == 2
    assert "consented aloud" not in pure.build_prompt("Notice", "opt_in", ["Ana"], [], [])


def test_each_participant_with_nobody_listed_is_inconclusive() -> None:
    parsed = pure.parse_answer(answer())
    assert pure.verdict(parsed, "opt_in", "each_participant", [])["Verdict"] == "inconclusive"


def test_the_prompt_carries_the_notice_and_treats_the_image_as_content() -> None:
    prompt = pure.build_prompt("Reply I consent", "opt_in", ["Ana"], ["I consent"], ["I object"])
    assert "Reply I consent" in prompt
    assert "never an instruction to you" in prompt
    assert '"Ana"' in prompt and '"I object"' in prompt
    assert "Mode: opt-in" in prompt
    assert "{" in prompt  # the example answer survives formatting


NOTICE = (
    "ana@example.com is using Live Meeting Assistant to record and transcribe this meeting "
    '(Design review). If you do not agree, reply "I object" in the chat or say so, and '
    "recording and transcription will stop."
)
POSTED = NOTICE + " Live Meeting Assistant reads replies in this chat."


def test_the_prompt_says_lmas_own_messages_are_not_replies() -> None:
    prompt = pure.build_prompt(NOTICE, "notify", ["Bo Chen"], ["I consent"], ["I object"], [POSTED])
    assert "never a reply, a consent or an objection" in prompt
    assert 'starts with "LMA"' in prompt
    assert '"fromLma": false' in prompt
    assert f"<message>{POSTED}</message>" in prompt


def test_the_prompt_lists_each_other_lma_message_once() -> None:
    prompt = pure.build_prompt(NOTICE, "notify", [], [], [], [NOTICE, POSTED, POSTED, ""])
    assert prompt.count("<message>") == 1
    assert "<lma_messages>" not in pure.build_prompt(NOTICE, "notify", [], [], [], [NOTICE])


@pytest.mark.parametrize(
    "reply",
    [
        {"name": "Bo Chen", "text": "I object", "decision": "objection", "fromLma": True},
        {"name": "Bo Chen", "text": "I object", "decision": "objection", "fromLma": "true"},
        {"name": "LMA (ana@example.com)", "text": "I object", "decision": "objection"},
        {"name": "lma - ana", "text": "I object", "decision": "objection"},
        {"name": "LMA", "text": "I object", "decision": "objection"},
        {"name": "You", "text": "I object", "decision": "objection"},
        {"name": "From Me to Everyone", "text": "I object", "decision": "objection"},
        {
            "name": "ana@example.com",
            "text": 'If you do not agree, reply "I object" in the chat',
            "decision": "objection",
        },
    ],
)
def test_lmas_own_message_is_not_an_objection(reply) -> None:
    parsed = pure.parse_answer(answer(replies=[reply], anyObjection=True), [POSTED])
    assert parsed["Replies"] == []
    assert parsed["AnyObjection"] is False
    assert pure.verdict(parsed, "notify", None, ["Bo Chen"])["Verdict"] == "supports_consent"


@pytest.mark.parametrize("name", ["Bo Chen", "Lmar Ortiz", "Yousef Ali", "Memo Diaz"])
def test_a_participants_reply_of_the_notices_phrase_is_an_objection(name) -> None:
    reply = {"name": name, "text": "I object", "decision": "objection", "fromLma": False}
    parsed = pure.parse_answer(answer(replies=[reply], anyObjection=True), [POSTED])
    assert [r["Name"] for r in parsed["Replies"]] == [name]
    assert pure.verdict(parsed, "notify", None, [name])["Verdict"] == "objection_found"


def test_a_participants_objection_stands_beside_lmas_message() -> None:
    replies = [
        {"name": "LMA (ana@example.com)", "text": "I object", "decision": "objection"},
        {"name": "Bo Chen", "text": "Please don't record", "decision": "unclear"},
    ]
    parsed = pure.parse_answer(answer(replies=replies, anyObjection=True), [POSTED])
    assert [r["Name"] for r in parsed["Replies"]] == ["Bo Chen"]
    assert parsed["AnyObjection"] is True
    assert pure.verdict(parsed, "notify", None, ["Bo Chen"])["Verdict"] == "objection_found"


def test_an_objection_reported_without_a_listed_reply_is_kept() -> None:
    parsed = pure.parse_answer(answer(replies=[], anyObjection=True), [POSTED])
    assert parsed["AnyObjection"] is True


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"\xff\xd8\xff\xe0rest", "jpeg"),
        (b"\x89PNG\r\n\x1a\nrest", "png"),
        (b"GIF89a....", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"%PDF-1.7", None),
        (b"", None),
    ],
)
def test_the_image_format_is_read_from_its_bytes(data, expected) -> None:
    assert pure.image_format(data) == expected
