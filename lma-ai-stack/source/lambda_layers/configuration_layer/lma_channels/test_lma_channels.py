# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The transcript channel names and the older spellings readers still accept."""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import lma_channels as ch  # noqa: E402

UI_TWIN = HERE.parents[2] / "ui" / "src" / "components" / "common" / "channels.js"


def test_the_speech_channels_are_remote_and_local() -> None:
    assert ch.SPEECH_CHANNELS == ("REMOTE", "LOCAL")


def test_the_older_names_read_as_the_current_ones() -> None:
    assert ch.canonical_channel("CALLER") == "REMOTE"
    assert ch.canonical_channel("CUSTOMER") == "REMOTE"
    assert ch.canonical_channel("AGENT") == "LOCAL"


def test_the_current_names_and_other_channels_are_unchanged() -> None:
    for channel in ("REMOTE", "LOCAL", "AGENT_ASSISTANT", "CHAT_ASSISTANT", "", None):
        assert ch.canonical_channel(channel) == channel


def test_only_the_two_speech_channels_are_speech_under_either_name() -> None:
    assert [c for c in ch.STORED_SPEECH_CHANNELS if ch.is_speech_channel(c)] == list(
        ch.STORED_SPEECH_CHANNELS
    )
    for channel in ("AGENT_ASSISTANT", "CHAT_ASSISTANT", "AGENT_VOICETONE", None, ""):
        assert not ch.is_speech_channel(channel)


def test_the_stored_speech_channels_are_every_name_that_reads_as_one() -> None:
    expected = set(ch.SPEECH_CHANNELS) | {
        old for old, new in ch.CHANNEL_ALIASES.items() if old != "CUSTOMER"
    }
    assert set(ch.STORED_SPEECH_CHANNELS) == expected


def test_a_speaker_key_is_rekeyed_by_its_channel_part_only() -> None:
    assert ch.canonical_speaker_key("CALLER#Other Participant (spk_0)") == (
        "REMOTE#Other Participant (spk_0)"
    )
    assert ch.canonical_speaker_key("AGENT#Me#1") == "LOCAL#Me#1"
    assert ch.canonical_speaker_key("REMOTE#CALLER") == "REMOTE#CALLER"
    assert ch.canonical_speaker_key("no channel") == "no channel"
    assert ch.canonical_speaker_key(None) is None


def test_a_section_is_rekeyed_and_the_current_name_wins() -> None:
    section = {
        "CALLER#a": {"name": "old"},
        "REMOTE#a": {"name": "new"},
        "AGENT#b": {"name": "Sam"},
    }
    assert ch.canonical_keyed(section) == {
        "REMOTE#a": {"name": "new"},
        "LOCAL#b": {"name": "Sam"},
    }


def test_merges_are_rekeyed_on_both_sides() -> None:
    assert ch.canonical_keyed({"CALLER#x": "CALLER#y"}, values_are_keys=True) == {
        "REMOTE#x": "REMOTE#y"
    }


def test_the_ui_twin_maps_the_same_names() -> None:
    """ui/src/components/common/channels.js must agree, or the screen and the
    summaries would disagree about which channel a stored segment is on."""
    text = UI_TWIN.read_text()
    found = {
        name: target.split("_")[0]
        for name, target in re.findall(r"^\s*(\w+): ((?:REMOTE|LOCAL)_CHANNEL),", text, re.M)
    }
    assert found == ch.CHANNEL_ALIASES
    assert "export const REMOTE_CHANNEL = 'REMOTE';" in text
    assert "export const LOCAL_CHANNEL = 'LOCAL';" in text
