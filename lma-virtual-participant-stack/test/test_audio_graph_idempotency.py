# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The PulseAudio graph in entrypoint.sh must be safe to build more than once.

A live MicroVM was observed with the ENTIRE audio graph duplicated::

    sinks:   meeting_audio  agent_output  combined_audio
             meeting_audio.2 agent_output.2 combined_audio.2
    sources: meeting_audio.monitor agent_output.monitor combined_audio.monitor
             agent_mic
             meeting_audio.2.monitor agent_output.2.monitor
             combined_audio.2.monitor agent_mic.2

``pactl`` renames a colliding sink instead of failing, so nothing complained.

The cause is the MicroVM lifecycle: ``bootStack()`` runs this script from the
``/ready`` hook, and Lambda RETRIES ``/ready`` whenever it answers 503 -- roughly
three times on a cold ARM boot, and ``bootStack`` reports failure if the VNC
ports come up slower than its timeout. Every ``load-module`` ran again on each
retry.

The spare sinks are inert (nothing writes to them). The damage is the second
``module-loopback`` from ``agent_output.monitor`` into ``combined_audio``: it
mixes the agent's voice in TWICE at two independent latencies, which is exactly
the double-writer entrypoint.sh warns about for meeting audio in GitHub #542 --
there, it made Transcribe emit every utterance twice ("Jack and Jill, Jack and
Jill went up ..."). And since ``/ready`` runs at IMAGE BUILD time, the duplicate
graph lands in the snapshot, so every launch inherits it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ENTRYPOINT = (
    Path(__file__).resolve().parents[1] / "backend" / "entrypoint.sh"
)


@pytest.fixture(name="entrypoint")
def entrypoint_fixture() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


@pytest.mark.parametrize("sink", ["meeting_audio", "agent_output", "combined_audio"])
def test_each_null_sink_is_guarded_by_an_existence_check(entrypoint: str, sink: str) -> None:
    """Creating a sink that already exists yields a renamed `.2` duplicate."""
    assert f"pa_sink_exists {sink}" in entrypoint, (
        f"module-null-sink {sink} must be skipped when it already exists"
    )


def test_the_agent_loopback_is_guarded(entrypoint: str) -> None:
    """The one duplicate that actually corrupts audio.

    Two loopbacks from the same monitor into the same sink sum the agent's voice
    twice, at two independent latencies -- the #542 double-writer, applied to the
    agent leg instead of the meeting leg.
    """
    assert "pa_loopback_exists agent_output.monitor combined_audio" in entrypoint


def test_the_agent_mic_remap_source_is_guarded(entrypoint: str) -> None:
    """A second agent_mic makes the meeting's microphone ambiguous.

    Chromium was seen requesting `deviceId: "default"`, so with both agent_mic and
    agent_mic.2 present, which one becomes the meeting's mic is PulseAudio's
    choice rather than ours.
    """
    assert "pa_source_exists agent_mic" in entrypoint


def test_the_guard_helpers_match_names_exactly(entrypoint: str) -> None:
    """`grep -qx` on the name column, not a substring match.

    A substring match would make `agent_output` satisfy a check for `agent_out`,
    and -- worse -- would let `agent_output.2` satisfy the check for
    `agent_output`, so a graph that was ALREADY duplicated would look correct.
    """
    for helper in ("pa_sink_exists", "pa_source_exists"):
        match = re.search(rf"{helper}\(\) \{{(.+?)\}}", entrypoint, re.S)
        assert match, f"{helper} should be defined"
        body = match.group(1)
        assert "cut -f2" in body, f"{helper} must look at the name column only"
        assert "grep -qx" in body, f"{helper} must match the whole name"


def test_the_default_source_is_pinned(entrypoint: str) -> None:
    """Otherwise the meeting's microphone is whatever PulseAudio ranks first.

    Every other reader names its device explicitly (the ffmpegs, the pacat
    streams, the speaking detector), so nothing depended on the previous
    behaviour -- but Chromium asked for "default" and got an unspecified source.
    """
    assert "pactl set-default-source agent_mic" in entrypoint


def test_the_default_sink_is_still_pinned(entrypoint: str) -> None:
    """Chromium's meeting audio has to land in meeting_audio, not a spare sink."""
    assert "pactl set-default-sink meeting_audio" in entrypoint


def test_meeting_audio_still_has_no_loopback_into_combined(entrypoint: str) -> None:
    """Guard the #542/#569 invariant while adding guards around it.

    meeting audio reaches combined_audio through the app's ACTIVE pacat stream
    only: a module-loopback there went digitally silent because it does not keep a
    null sink out of suspend (#569), and a second writer duplicated every
    utterance to Transcribe (#542).
    """
    loopbacks: list[str] = re.findall(r"module-loopback[^\n]*", entrypoint)
    for line in loopbacks:
        assert "source=meeting_audio.monitor" not in line, (
            f"meeting_audio must not be loopback-routed into combined_audio: {line}"
        )
