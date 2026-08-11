# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Static tests that the VP audio path is 16 kHz mono end to end (GitHub #538).

The voice assistant sounded warbly in a live Teams meeting. ffmpeg reported the
PulseAudio sinks as `48000 Hz, stereo` while every stage of the pipeline — Nova
Sonic input and output, Transcribe, and the pacat playback streams — is 16 kHz
mono PCM16. PulseAudio's compiled default is 48 kHz stereo and the null sinks were
created without a rate, so they inherited it.

Nova's voice was therefore resampled at least three times before the meeting heard
it:

    Nova 16k -> agent_output (48k)          <- resample 1
    agent_output.monitor -> combined_audio  <- resample 2 (loopback)
    agent_output.monitor -> agent_mic       <- resample 3 (remap-source)

Cumulative interpolation on a 3x upsample is audible as warble, with no gaps
because the timing was never the problem. Transcription was unaffected in quality
because speech recognition tolerates resampling far better than a human ear.

These assertions are cheap and catch the regression at commit time rather than
requiring a live meeting and someone listening carefully.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
ENTRYPOINT = BACKEND / "entrypoint.sh"
DOCKERFILE = BACKEND / "Dockerfile"

# The pipeline's native format. Nova Sonic's audioInput/audioOutputConfiguration
# both declare sampleRateHertz 16000, channelCount 1, sampleSizeBits 16.
PIPELINE_RATE = "16000"


@pytest.fixture(scope="module")
def entrypoint() -> str:
    return ENTRYPOINT.read_text()


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text()


def _null_sink_lines(entrypoint: str) -> list[str]:
    return [
        line.strip()
        for line in entrypoint.splitlines()
        if "module-null-sink" in line and not line.strip().startswith("#")
    ]


def test_all_three_null_sinks_are_created(entrypoint: str) -> None:
    """meeting_audio, agent_output and combined_audio."""
    lines = _null_sink_lines(entrypoint)
    assert len(lines) == 3, f"expected 3 null sinks, found {len(lines)}"
    names = {re.search(r"sink_name=(\w+)", line).group(1) for line in lines}
    assert names == {"meeting_audio", "agent_output", "combined_audio"}


def test_every_null_sink_pins_rate_and_channels(entrypoint: str) -> None:
    """A sink without rate= adopts the daemon default (48 kHz stereo).

    That is the specific defect behind #538: the sink format silently disagreed
    with every producer and consumer attached to it.
    """
    for line in _null_sink_lines(entrypoint):
        name = re.search(r"sink_name=(\w+)", line).group(1)
        assert f"rate={PIPELINE_RATE}" in line, f"{name} does not pin rate={PIPELINE_RATE}"
        assert "channels=1" in line, f"{name} does not pin channels=1"
        assert "format=s16le" in line, f"{name} does not pin format=s16le"


def test_daemon_config_pins_the_pipeline_format(dockerfile: str) -> None:
    """Belt and braces alongside the per-sink settings.

    Pinning the daemon covers anything that creates a stream without an explicit
    spec — including PulseAudio's own internal paths.
    """
    assert f"default-sample-rate = {PIPELINE_RATE}" in dockerfile
    # alternate-sample-rate is what PulseAudio switches to for streams that do
    # not match the default; leaving it at 44100 would reintroduce a resample.
    assert f"alternate-sample-rate = {PIPELINE_RATE}" in dockerfile
    assert "default-sample-channels = 1" in dockerfile


def test_resampler_favours_quality_over_speed(dockerfile: str) -> None:
    """PulseAudio defaults to speex-float-1, tuned for speed.

    Any residual conversion should not add avoidable artifacts to speech.
    """
    # The Dockerfile writes these via printf, so each line is single-quoted.
    match = re.search(r"resample-method = ([a-z0-9-]+)", dockerfile)
    assert match, "resample-method should be set explicitly"
    method = match.group(1)
    assert method != "speex-float-1", "the speed-tuned default is not appropriate here"
    assert method.startswith("speex-float-"), f"unexpected resampler {method}"
    assert int(method.rsplit("-", 1)[1]) >= 3, "quality level should be >= 3"


def test_avoid_resampling_is_enabled(dockerfile: str) -> None:
    """Makes PulseAudio follow the stream's rate instead of converting."""
    assert "avoid-resampling = yes" in dockerfile


def test_daemon_config_is_owned_by_the_running_user(dockerfile: str) -> None:
    """The daemon runs as appuser, so the config must be readable by it.

    A root-owned file in /home/appuser/.config would be silently ignored and the
    48 kHz default would come straight back.
    """
    assert "/home/appuser/.config/pulse/daemon.conf" in dockerfile
    chown = re.search(r"chown -R appuser:appuser ([^\n]+)", dockerfile)
    assert chown, "expected a chown for appuser"
    assert "/home/appuser/.config" in chown.group(1)


def test_entrypoint_warns_if_the_sinks_are_not_16k_mono(entrypoint: str) -> None:
    """A regression should be loud in the logs, not merely audible.

    Without this, the only symptom is degraded voice-assistant quality that takes
    a live meeting and attentive listening to notice.
    """
    assert "16000Hz" in entrypoint
    assert "WARNING" in entrypoint
    # Must write the warning to stderr so it is not lost among routine output.
    assert re.search(r"WARNING: audio sinks are NOT 16 kHz mono.*>&2", entrypoint)


def test_pacat_streams_use_the_pipeline_rate() -> None:
    """The playback stream either side of the sinks must agree with them.

    nova-agent.ts plays Nova's output into agent_output, and ffmpeg captures the
    monitors at 16 kHz; a mismatch would just move the resample rather than
    remove it.

    scribe.ts is deliberately NOT checked here: its pacat pipe into
    combined_audio was removed as the duplicate writer behind #542. See
    test_audio_single_writer.py, which asserts it stays removed.
    """
    nova = (BACKEND / "src" / "nova-agent.ts").read_text()
    scribe = (BACKEND / "src" / "scribe.ts").read_text()
    assert f"NOVA_PLAYBACK_RATE || '{PIPELINE_RATE}'" in nova
    # ffmpeg still captures the monitors at the pipeline rate.
    assert f"'-ar', '{PIPELINE_RATE}'" in scribe or f"String(this.sampleRate)" in scribe
