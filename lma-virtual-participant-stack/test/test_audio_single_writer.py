# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Each audio sink must have exactly ONE writer (GitHub #542, #543).

Meeting audio reached `combined_audio` by two independent routes:

    entrypoint.sh   module-loopback meeting_audio.monitor -> combined_audio
    scribe.ts       ffmpeg(meeting_audio.monitor) -> pacat --device=combined_audio

Both delivered the same speech at different latencies, so Amazon Transcribe heard
every utterance twice and transcribed both:

    "Jack and Jill, Jack and Jill went up the peal of water, peel of water ..."

The duplicate route had existed since 2026-03-18 but was masked: `combined_audio`
ran at the daemon default (48 kHz stereo) and each route was independently
resampled from 16 kHz, which smeared and attenuated the second copy enough that
Transcribe discarded it as echo. Pinning the sinks to native 16 kHz mono (#538)
made both copies crisp and in lockstep, which is when the duplication appeared.

The asymmetry is what identified it: `agent_output` has only ONE route into
`combined_audio`, so the assistant's own speech never duplicated -- only meeting
audio did.

These are static checks, because the failure is invisible in the container's own
logs: both routes look healthy, and the only symptom is a transcript nobody is
reading at the time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
ENTRYPOINT = BACKEND / "entrypoint.sh"
SCRIBE = BACKEND / "src" / "scribe.ts"
NOVA = BACKEND / "src" / "nova-agent.ts"

# Audio buffers must tolerate scheduling jitter on a 2-vCPU MicroVM running
# Chromium + avatar rescale + video recording + Transcribe concurrently. 20ms
# underran audibly (crackling); 80ms is still well below conversational latency.
MIN_LATENCY_MS = 80


def _uncommented(path: Path) -> list[str]:
    """Lines with comment-only lines removed.

    The files deliberately document the removed second writer in comments; those
    must not count as a writer.
    """
    out = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*"):
            continue
        out.append(line)
    return out


@pytest.fixture(scope="module")
def entrypoint_lines() -> list[str]:
    return _uncommented(ENTRYPOINT)


def test_no_loopback_writes_meeting_audio_into_combined(
    entrypoint_lines: list[str],
) -> None:
    """A module-loopback is NOT a reliable route into a null sink.

    It does not keep the sink out of PulseAudio's idle/suspend state, so this
    route went digitally silent: a live Zoom session recorded 50s of
    combined_audio with peak amplitude 0 while meeting_audio itself had audio
    (GitHub #569). The app's pacat stream is the route instead -- it delivers the
    audio AND keeps the sink running.

    This is the inverse of the original #542 fix, which removed the wrong writer.
    """
    writers = [
        line
        for line in entrypoint_lines
        if "module-loopback" in line
        and "source=meeting_audio.monitor" in line
        and "sink=combined_audio" in line
    ]
    assert not writers, (
        "entrypoint.sh must NOT loopback meeting_audio into combined_audio; the "
        f"pacat stream in scribe.ts is the sole route. Found: {writers}"
    )


def test_the_app_is_the_sole_writer_of_meeting_audio_into_combined() -> None:
    """scribe.ts holds exactly one ACTIVE pacat stream on combined_audio.

    "Active" is the load-bearing word: it is what keeps the null sink from
    suspending. Exactly one, because two writers made Transcribe hear every
    utterance twice ("Jack and Jill, Jack and Jill went up...", #542).
    """
    scribe = "\n".join(_uncommented(SCRIBE))
    assert scribe.count("--device=combined_audio") == 1, (
        "expected exactly one pacat writer into combined_audio in scribe.ts"
    )
    assert "meetingToCombinedPipe.stdin.write(chunk)" in scribe, (
        "the pipe must actually be fed, not merely spawned"
    )


def test_the_pacat_writer_uses_an_active_stream_not_a_oneshot() -> None:
    """The stream must be long-lived, since keeping the sink awake is its job.

    A short-lived writer would deliver its chunk and let combined_audio suspend
    again, reproducing #569 intermittently — which is the worst version of this
    bug, because it looks like it works.
    """
    scribe = SCRIBE.read_text()
    spawn_idx = scribe.index("--device=combined_audio")
    # The handle is retained on the instance and fed from the ffmpeg data handler,
    # rather than spawned per chunk.
    assert "this.meetingToCombinedPipe = spawn('pacat'" in scribe
    assert scribe.count("spawn('pacat'") >= 1
    # ...and torn down with the session, not leaked.
    assert "this.meetingToCombinedPipe.kill()" in scribe
    assert spawn_idx > 0


def test_agent_audio_also_has_exactly_one_route_into_combined(
    entrypoint_lines: list[str],
) -> None:
    """Kept symmetric with the meeting route.

    agent_output having a single route is why the assistant's own speech never
    duplicated -- that asymmetry is what diagnosed #542, so it is worth pinning.
    """
    writers = [
        line
        for line in entrypoint_lines
        if "module-loopback" in line
        and "source=agent_output.monitor" in line
        and "sink=combined_audio" in line
    ]
    assert len(writers) == 1


def test_only_nova_writes_into_agent_output() -> None:
    """The assistant's voice must reach the meeting by one path only.

    Two copies of the same speech summed at an offset is comb filtering, which is
    audible as warble even when nothing is dropped.
    """
    nova = "\n".join(_uncommented(NOVA))
    writers = re.findall(r"--device=agent_output", nova)
    assert len(writers) == 1, f"expected 1 writer into agent_output, found {len(writers)}"
    scribe = "\n".join(_uncommented(SCRIBE))
    assert "--device=agent_output" not in scribe


def test_pulse_loopback_latency_tolerates_cpu_jitter(entrypoint_lines: list[str]) -> None:
    for line in entrypoint_lines:
        # Only lines that CREATE a loopback. entrypoint.sh also greps the module
        # list for an existing loopback to stay idempotent under /ready retries
        # (see test_audio_graph_idempotency.py), and that detection pattern
        # naturally mentions module-loopback without setting a latency.
        if "load-module module-loopback" not in line:
            continue
        match = re.search(r"latency_msec=(\d+)", line)
        assert match, f"loopback should set latency_msec explicitly: {line.strip()}"
        assert int(match.group(1)) >= MIN_LATENCY_MS, (
            f"latency_msec={match.group(1)} underran on a 2-vCPU MicroVM; "
            f"expected >= {MIN_LATENCY_MS}"
        )


def test_nova_playback_buffer_tolerates_cpu_jitter() -> None:
    """20ms produced audible crackling under concurrent load."""
    nova = NOVA.read_text()
    match = re.search(r"NOVA_PLAYBACK_LATENCY_MS \|\| '(\d+)'", nova)
    assert match, "the playback latency should be configurable with a sane default"
    assert int(match.group(1)) >= MIN_LATENCY_MS
    # And the literal 20ms must be gone.
    assert "--latency-msec=20" not in nova


def test_avatar_rescale_does_not_clear_the_full_canvas_every_frame() -> None:
    """A 1920x1080 fillRect at 15fps starved PulseAudio on 2 vCPUs.

    Teams' ACS SDK is the only caller requesting `exact` dimensions, so it is the
    only one on the rescale path — which is why the crackle was Teams-only while
    Zoom, with identical sinks, sounded clean. That asymmetry is what ruled out
    resampling as the cause.
    """
    avatar = (BACKEND / "src" / "simli-avatar.ts").read_text()
    draw = avatar[avatar.index("const draw = () => {") : avatar.index("timer = setInterval(draw")]
    # The fill must be guarded by a geometry-change check, not run unconditionally.
    assert "fillRect" in draw, "the letterbox still needs painting once"
    assert "lastSrcW" in draw, "geometry should be cached across frames"
    fill_line = next(i for i, l in enumerate(draw.splitlines()) if "fillRect" in l)
    guard_line = next(i for i, l in enumerate(draw.splitlines()) if "!== lastSrcW" in l)
    assert guard_line < fill_line, "fillRect must sit inside the geometry-change branch"
