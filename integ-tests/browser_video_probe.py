# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Screen video from the Stream Audio/Video page and the browser extension, at the protocol.

The two browser clients send their video the way the Desktop Capture App does:
a second WebSocket with ``START_VIDEO``, fragmented-MP4 chunks and
``END_VIDEO`` (docs/websocket-streaming-api.md, "Screen Video"). What differs is
on the server: their video is accepted only when Meeting Recording › Record
screen video from Stream Audio/Video and the browser extension is on, and their
key frames are recorded with the client's own ``Source``. This probe streams
the narrated slideshow ``desktop_video_probe.py`` builds, as one of those
clients (the START's ``clientType``), and reports what each connection saw:
how it ended, the consent frames on the audio connection, and how many video
fragments went out. The tests read the stored meeting afterwards.

Each run is one capture session ("leg"): a START (optionally with a consent
step, or a ``sessionNumber`` to continue a meeting), the audio in real time,
and the video on its own connection, optionally cut short after a number of
fragments, optionally waiting for the consent grant before ``START_VIDEO``, and
optionally reporting an objection part-way through.

Depends on what ``desktop_video_probe.py`` depends on. Used by
``test_browser_video.py``; nothing is deleted afterwards (the meetings expire
with the deployment's retention).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from desktop_video_probe import (
    KEYFRAME_SECONDS,
    LEAD_SECONDS,
    SAMPLE_RATE,
    TAIL_SECONDS,
    Media,
)

#: How long a leg waits for the consent grant before sending START_VIDEO anyway.
GRANT_TIMEOUT_SECONDS = 20.0
#: How long after an objection the server has to close the connections.
OBJECTION_CLOSE_SECONDS = 10.0


@dataclass
class Leg:
    """What one capture session's two connections saw."""

    call_id: str
    client_type: str
    session_number: int | None = None
    audio_state: str | None = None
    audio_close_code: int | None = None
    audio_close_reason: str | None = None
    video_started: bool = False
    # Frames the transcriber sent on the video connection (VIDEO_STARTED).
    video_frames: list = field(default_factory=list)
    video_state: str | None = None
    video_close_code: int | None = None
    video_close_reason: str | None = None
    video_offset_seconds: float = LEAD_SECONDS
    fragments_planned: int = 0
    fragments_sent: int = 0
    frames: list = field(default_factory=list)
    granted_at: float | None = None
    objection_sent_at: float | None = None
    video_closed_at: float | None = None
    end_sent: bool = False
    end_video_sent: bool = False

    def consent_states(self) -> list:
        return [f for f in self.frames if f.get("event") == "CONSENT_STATE"]

    def codes(self) -> list:
        return [f.get("code") for f in self.consent_states() if f.get("code")]

    def summary(self) -> str:
        return json.dumps(
            {k: v for k, v in self.__dict__.items() if k != "frames"} | {"codes": self.codes()},
            default=str,
        )


def new_call_id(label: str) -> str:
    return f"browser-video-probe-{label}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"


def ws_url(endpoint: str, tokens: dict) -> str:
    return (
        f"{endpoint}?authorization=Bearer%20{tokens['access']}"
        f"&id_token={tokens['id']}&refresh_token={tokens['refresh']}"
    )


async def _settings_answer(url: str, client_type: str | None) -> dict | None:
    import websockets

    frame: dict[str, Any] = {"callEvent": "CONSENT_SETTINGS"}
    if client_type:
        frame["clientType"] = client_type
    async with websockets.connect(url, max_size=None, open_timeout=25) as ws:
        await ws.send(json.dumps(frame))
        deadline = time.monotonic() + 15
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(ws.recv(), remaining)
            except TimeoutError:
                return None
            if isinstance(raw, str) and json.loads(raw).get("event") == "CONSENT_SETTINGS":
                return json.loads(raw)
    return None


def settings_answer(endpoint: str, tokens: dict, client_type: str | None) -> dict | None:
    """The transcriber's CONSENT_SETTINGS answer for this client, or None if it gives none."""
    return asyncio.run(_settings_answer(ws_url(endpoint, tokens), client_type))


async def _stream_leg(url: str, start: dict, media: Media, leg: Leg, *, fragments: int | None,
                      wait_for_grant: bool, objection_after_fragments: int | None) -> None:
    import websockets

    init, frags = media.fragments[0], media.fragments[1:]
    if fragments is not None:
        frags = frags[:fragments]
    leg.fragments_planned = len(frags)
    # Audio for the video sent, plus the lead and a tail, so every slide shown
    # is narrated and the last audio block is flushed.
    audio_seconds = LEAD_SECONDS + len(frags) * KEYFRAME_SECONDS + TAIL_SECONDS
    pcm = media.pcm[: int(audio_seconds * SAMPLE_RATE) * 4]
    chunk_ms = 200
    chunk_bytes = SAMPLE_RATE * chunk_ms // 1000 * 4
    granted = asyncio.Event()
    audio_closed = asyncio.Event()

    async with websockets.connect(url, max_size=None, open_timeout=25) as audio_ws:
        await audio_ws.send(json.dumps(start))
        t0 = time.monotonic()

        async def receive() -> None:
            try:
                async for raw in audio_ws:
                    if not isinstance(raw, str):
                        continue
                    frame = json.loads(raw)
                    leg.frames.append(frame)
                    if frame.get("event") == "CONSENT_STATE" and frame.get("status") == "granted":
                        leg.granted_at = time.monotonic()
                        granted.set()
            except websockets.ConnectionClosed:
                pass
            finally:
                audio_closed.set()

        async def audio() -> None:
            try:
                for i, pos in enumerate(range(0, len(pcm), chunk_bytes)):
                    if audio_closed.is_set():
                        return
                    await audio_ws.send(pcm[pos:pos + chunk_bytes])
                    delay = t0 + (i + 1) * chunk_ms / 1000 - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
            except websockets.ConnectionClosed:
                return

        async def video() -> None:
            await asyncio.sleep(LEAD_SECONDS / 2)
            if wait_for_grant:
                try:
                    await asyncio.wait_for(granted.wait(), GRANT_TIMEOUT_SECONDS)
                except TimeoutError:
                    pass
            if audio_closed.is_set():
                return
            async with websockets.connect(url, max_size=None, open_timeout=25) as video_ws:
                video_start = time.monotonic()
                leg.video_offset_seconds = round(video_start - t0, 3)
                reader = None
                try:
                    await video_ws.send(json.dumps({
                        "callEvent": "START_VIDEO", "callId": leg.call_id,
                        "videoTimeOffsetMs": int(leg.video_offset_seconds * 1000),
                    }))
                    leg.video_started = True

                    async def read_frames() -> None:
                        try:
                            async for raw in video_ws:
                                if isinstance(raw, str):
                                    leg.video_frames.append(json.loads(raw))
                        except websockets.ConnectionClosed:
                            pass

                    reader = asyncio.create_task(read_frames())
                    for i, frag in enumerate(frags):
                        # A fragment goes out once its two seconds have been
                        # captured, the init segment with the first, as the
                        # browser's MediaRecorder delivers them.
                        delay = video_start + (i + 1) * KEYFRAME_SECONDS - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        if i == 0:
                            await video_ws.send(init)
                        await video_ws.send(frag)
                        leg.fragments_sent += 1
                        if objection_after_fragments is not None and leg.fragments_sent == objection_after_fragments:
                            await audio_ws.send(json.dumps({
                                "callEvent": "CONSENT_OBJECTION", "participantName": "Bo Chen",
                                "source": "chat", "text": "I object",
                            }))
                            leg.objection_sent_at = time.monotonic()
                    if leg.objection_sent_at is not None:
                        # The server closes the video connection itself.
                        await asyncio.wait_for(video_ws.wait_closed(), OBJECTION_CLOSE_SECONDS)
                    else:
                        leg.video_state = video_ws.state.name
                        await video_ws.send(json.dumps({"callEvent": "END_VIDEO", "callId": leg.call_id}))
                        leg.end_video_sent = True
                        await asyncio.sleep(1.0)
                except (TimeoutError, websockets.ConnectionClosed):
                    pass
                if reader is not None:
                    reader.cancel()
                if leg.video_state is None:
                    leg.video_state = video_ws.state.name
            leg.video_closed_at = time.monotonic()
            leg.video_close_code = video_ws.close_code
            leg.video_close_reason = video_ws.close_reason

        receiver = asyncio.create_task(receive())
        await asyncio.gather(audio(), video())
        if not audio_closed.is_set():
            leg.audio_state = audio_ws.state.name
            try:
                await audio_ws.send(json.dumps({"callEvent": "END", "callId": leg.call_id}))
                leg.end_sent = True
                await asyncio.sleep(1.0)
            except websockets.ConnectionClosed:
                pass
        else:
            leg.audio_state = "CLOSED_BY_SERVER"
        await audio_ws.close()
        receiver.cancel()
    leg.audio_close_code = audio_ws.close_code
    leg.audio_close_reason = audio_ws.close_reason


def stream_leg(endpoint: str, tokens: dict, username: str, media: Media, *, client_type: str,
               call_id: str, session_number: int | None = None, consent: dict | None = None,
               fragments: int | None = None, wait_for_grant: bool = False,
               objection_after_fragments: int | None = None) -> Leg:
    """Stream one capture session as ``client_type``; returns what its connections saw."""
    start: dict[str, Any] = {
        "callEvent": "START", "callId": call_id, "clientType": client_type,
        "remoteSpeakerName": "Browser Video Probe", "localSpeakerName": username,
        "samplingRate": SAMPLE_RATE,
    }
    if session_number is not None:
        start["sessionNumber"] = session_number
    if consent is not None:
        start["consent"] = consent
    leg = Leg(call_id=call_id, client_type=client_type, session_number=session_number)
    asyncio.run(_stream_leg(ws_url(endpoint, tokens), start, media, leg, fragments=fragments,
                            wait_for_grant=wait_for_grant, objection_after_fragments=objection_after_fragments))
    return leg


def expected_timestamps(media: Media, leg: Leg, base_seconds: float = 0.0) -> list[float]:
    """Where each slide the leg showed should land on the transcript's clock.

    A slide counts as shown when its first two seconds went out; ``base_seconds``
    is where the leg's session starts on the meeting's timeline.
    """
    shown_seconds = leg.fragments_sent * KEYFRAME_SECONDS
    return [base_seconds + leg.video_offset_seconds + s for s in media.starts if s + KEYFRAME_SECONDS <= shown_seconds]
