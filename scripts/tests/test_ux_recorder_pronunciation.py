# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Pronunciation hints change what Polly is asked to say, never the captions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ux_recorder_render as render  # noqa: E402

LIVE = [{"phrase": "Live Meeting Assistant", "word": "Live", "ipa": "laɪv"}]


def test_the_hinted_word_is_wrapped_in_a_phoneme_wherever_the_phrase_appears():
    ssml = render.to_ssml(
        "Live Meeting Assistant joins. That is Live Meeting Assistant.",
        pronunciations=LIVE,
    )
    assert ssml.count('<phoneme alphabet="ipa" ph="laɪv">Live</phoneme> Meeting Assistant') == 2


def test_the_word_outside_the_phrase_is_left_alone():
    ssml = render.to_ssml("We translate the transcript live.", pronunciations=LIVE)
    assert "<phoneme" not in ssml


def test_without_hints_the_ssml_is_unchanged():
    text = "Live Meeting Assistant joins."
    assert render.to_ssml(text) == render.to_ssml(text, pronunciations=[])
    assert "<phoneme" not in render.to_ssml(text)


def test_text_is_still_escaped_around_a_hint():
    ssml = render.to_ssml("Live Meeting Assistant & friends <3", pronunciations=LIVE)
    assert "&amp; friends &lt;3" in ssml and "<phoneme" in ssml


def test_a_hint_changes_the_cache_key_only_of_lines_it_applies_to():
    line = "Live Meeting Assistant joins."
    assert render.narration_cache_key(
        "generative", "Ruth", line, "ssml"
    ) != render.narration_cache_key("generative", "Ruth", line, "ssml", LIVE)
    other = "We translate the transcript live."
    assert render.narration_cache_key(
        "generative", "Ruth", other, "ssml"
    ) == render.narration_cache_key("generative", "Ruth", other, "ssml", LIVE)


def test_the_session_file_is_read_and_checked(tmp_path):
    assert render.load_pronunciations(tmp_path) == []
    (tmp_path / render.PRONUNCIATIONS_FILE).write_text(json.dumps(LIVE), encoding="utf-8")
    assert render.load_pronunciations(tmp_path) == LIVE
    (tmp_path / render.PRONUNCIATIONS_FILE).write_text(
        json.dumps([{"phrase": "Live Meeting Assistant", "word": "Hive", "ipa": "x"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        render.load_pronunciations(tmp_path)
