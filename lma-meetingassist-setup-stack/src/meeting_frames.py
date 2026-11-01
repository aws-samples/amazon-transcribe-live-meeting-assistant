# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The meeting assistant's view of the shared screen: the meeting's key frames.

A key frame is a picture of the shared screen kept each time it changed and held
still (docs/meeting-video.md). The ``get_meeting_frames`` tool hands the agent
the frames a question is about -- the last few, those in a time range, or the
one on screen at a moment -- each with what was said while it was on screen,
and, for a model that reads images, the pictures themselves.

Frames are read through the ``getVideoKeyframes`` query, which signs short-lived
links to the images; this function's role signs with IAM and is answered as the
backend. The images are downloaded from those links and passed to the model as
bytes.

The decisions are kept free of I/O so they can be tested; ``load_keyframes``
and ``download_image`` are the two calls out.
"""

from __future__ import annotations

import logging
import re
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Frames in one answer. More than a handful is never what a question needs,
#: and each image is over a thousand input tokens on most models.
MAX_FRAMES = 10
#: Of those, how many go to the model as pictures; the rest are described in text.
MAX_IMAGES = 4
#: What was said while a frame was on screen, per frame.
MAX_TRANSCRIPT_CHARS = 1500
#: Largest image passed to the model (Bedrock accepts up to 3.75 MB).
MAX_IMAGE_BYTES = 3_500_000

KEYFRAMES_QUERY = """
query GetVideoKeyframes($callId: ID!, $limit: Int, $nextToken: String) {
  getVideoKeyframes(callId: $callId, limit: $limit, nextToken: $nextToken) {
    VideoKeyframes { FrameId Timestamp VideoOffset Width Height ImageSignedUrl }
    nextToken
  }
}
"""

NO_FRAMES = (
    "This meeting has no key frames: nothing was screen-shared while its video was "
    "being recorded, or its video is not recorded. Say so; do not describe slides "
    "you cannot see."
)


def model_accepts_images(model_id: str) -> bool:
    """Whether the agent's model reads images in a tool result.

    Of the models the stack offers, Amazon Nova Micro is the one that is text
    only: Bedrock rejects an image block sent to it. Every Claude model and the
    other Nova models read images.
    """
    return "nova-micro" not in (model_id or "").lower()


_CLOCK = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)\s*$")
_SECONDS = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*s?\s*$", re.IGNORECASE)


def parse_meeting_time(text: Any) -> Optional[float]:
    """Seconds from the start of the meeting, from "12:30", "1:02:03", "754" or "754s"."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text) if text >= 0 else None
    value = str(text).strip().strip("[]")
    if not value:
        return None
    match = _CLOCK.match(value)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
    match = _SECONDS.match(value)
    if match:
        return float(match.group(1))
    return None


def format_time(seconds: Any) -> str:
    """A meeting time as the transcript shows it: MM:SS, or H:MM:SS from an hour."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "--:--"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def with_spans(frames: Sequence[Mapping[str, Any]]) -> List[Tuple[Mapping[str, Any], Optional[float]]]:
    """Each frame in time order with the time the next one replaced it (None for the last)."""
    ordered = sorted(
        (f for f in frames if isinstance(f.get("Timestamp"), (int, float))),
        key=lambda f: f["Timestamp"],
    )
    return [
        (frame, ordered[i + 1]["Timestamp"] if i + 1 < len(ordered) else None)
        for i, frame in enumerate(ordered)
    ]


def select_frames(
    frames: Sequence[Mapping[str, Any]],
    *,
    last: int = 0,
    start: Optional[float] = None,
    end: Optional[float] = None,
    at: Optional[float] = None,
) -> List[Tuple[Mapping[str, Any], Optional[float]]]:
    """The frames a question is about, each with when it left the screen.

    ``at``: the one on screen at that time. ``start``/``end``: those on screen at
    any point in the range. Otherwise the last ``last`` (at least one). At most
    MAX_FRAMES, keeping the latest.
    """
    spans = with_spans(frames)
    if not spans:
        return []
    if at is not None:
        chosen = [s for s in spans if s[0]["Timestamp"] <= at] or spans[:1]
        return chosen[-1:]
    if start is not None or end is not None:
        lo = start if start is not None else 0.0
        hi = end if end is not None else float("inf")
        chosen = [s for s in spans if s[0]["Timestamp"] <= hi and (s[1] is None or s[1] > lo)]
        return chosen[-MAX_FRAMES:]
    count = max(1, min(MAX_FRAMES, int(last or 1)))
    return spans[-count:]


def transcript_between(
    segments: Iterable[Mapping[str, Any]], start: float, end: Optional[float]
) -> str:
    """What was said between two times, one "[MM:SS] Speaker: text" line per segment."""
    lines: List[str] = []
    timed: List[Tuple[float, Mapping[str, Any]]] = []
    for seg in segments:
        # Coerced, not type-checked: segments read through the DynamoDB resource
        # API carry their times as Decimal.
        try:
            timed.append((float(seg.get("StartTime")), seg))
        except (TypeError, ValueError):
            continue
    for t, seg in sorted(timed, key=lambda pair: pair[0]):
        if t < start or (end is not None and t >= end):
            continue
        speaker = "MeetingAssistant" if seg.get("Channel") == "AGENT_ASSISTANT" else seg.get("Speaker") or "Unknown"
        lines.append(f"[{format_time(t)}] {speaker}: {seg.get('Transcript', '')}")
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[: MAX_TRANSCRIPT_CHARS - 20].rsplit("\n", 1)[0] + "\n[... more not shown]"
    return text


def frames_for_pictures(
    selected: Sequence[Tuple[Mapping[str, Any], Optional[float]]],
    *,
    send_images: bool,
    wants: Callable[[Mapping[str, Any]], bool] = lambda _frame: True,
) -> List[Mapping[str, Any]]:
    """The frames whose pictures are sent: the newest MAX_IMAGES of those that want one.

    The one place the picture budget is spent, so the frames downloaded and the
    frames build_tool_result describes as pictured are the same frames.
    """
    if not send_images:
        return []
    wanting = [frame for frame, _ in selected if wants(frame)]
    return wanting[-MAX_IMAGES:]


def frame_markdown(frame: Mapping[str, Any]) -> str:
    """How an answer shows a frame in the meeting page's chat: resolved there to a signed link."""
    return f"![Shared screen at {format_time(frame.get('Timestamp'))}](keyframe:{frame.get('FrameId')})"


def build_tool_result(
    selected: Sequence[Tuple[Mapping[str, Any], Optional[float]]],
    segments: Iterable[Mapping[str, Any]],
    images: Mapping[str, bytes],
    *,
    total_frames: int,
    send_images: bool,
    show_inline: bool,
    at_time: Optional[float] = None,
) -> Dict[str, Any]:
    """The Strands tool result: a text block per frame and, where allowed, its image.

    ``images`` holds the downloaded pictures by FrameId; a frame without one is
    described in text only. ``show_inline`` adds how to show a frame in the
    meeting page's chat, which only that chat can display. ``at_time`` is the
    moment asked about, if any, so a moment before anything was shared is said
    to be one.
    """
    if not selected:
        return {"status": "success", "content": [{"text": NO_FRAMES}]}
    segment_list = list(segments)
    content: List[Dict[str, Any]] = []
    header = [
        f"{len(selected)} of the meeting's {total_frames} key frame(s) of the shared screen, "
        "oldest first. Each is listed with the time it went up, how long it stayed, and what "
        "was said while it was on screen."
    ]
    if send_images:
        header.append(
            "The pictures follow their descriptions. Read what is on them to answer: titles, "
            "text, figures. Describe only what is shown; do not identify people from their faces."
        )
    else:
        header.append(
            "The pictures cannot be shown to this model, so answer from the times and the "
            "transcript, and say that the slide itself could not be read."
        )
    if show_inline:
        header.append(
            "To show a frame to the user, put its image markdown, exactly as given, on a line "
            "of its own in the answer."
        )
    else:
        header.append("This answer is text only: describe frames in words, with no image markdown.")
    content.append({"text": "\n".join(header)})

    pictured = {str(f.get("FrameId")) for f in frames_for_pictures(selected, send_images=send_images)}
    before_first = at_time is not None and float(selected[0][0]["Timestamp"]) > at_time
    if before_first:
        content.append({"text": (
            f"Nothing had been shared yet at {format_time(at_time)}; this is the first frame, "
            "shown after that."
        )})
    for frame, until in selected:
        frame_id = str(frame.get("FrameId"))
        went_up = float(frame["Timestamp"])
        span = f"from {format_time(went_up)} to {format_time(until)}" if until is not None else (
            f"from {format_time(went_up)} (still the latest)"
        )
        lines = [f"Frame {frame_id}: on screen {span}."]
        if show_inline:
            lines.append(f"Image markdown: {frame_markdown(frame)}")
        said = transcript_between(segment_list, went_up, until)
        lines.append(f"What was said while it was on screen:\n{said}" if said else "Nothing was said while it was on screen.")
        image = images.get(frame_id) if frame_id in pictured else None
        if send_images and frame_id not in pictured:
            lines.append(f"(Picture not included: at most {MAX_IMAGES} are sent at once.)")
        elif send_images and image is None:
            lines.append("(The picture of this frame could not be read.)")
        content.append({"text": "\n".join(lines)})
        if image is not None:
            content.append({"image": {"format": "jpeg", "source": {"bytes": image}}})
    return {"status": "success", "content": content}


def images_in(result: Mapping[str, Any]) -> int:
    """How many pictures a tool result carries, for the cost estimate."""
    return sum(1 for block in result.get("content", []) if "image" in block)


def load_keyframes(execute: Callable[[str, Dict[str, Any]], Mapping[str, Any]], call_id: str) -> List[Dict[str, Any]]:
    """Every key frame of the meeting, through getVideoKeyframes, in time order."""
    frames: List[Dict[str, Any]] = []
    token = None
    for _ in range(20):  # 20 pages of 100 is twice the per-meeting cap
        response = execute(KEYFRAMES_QUERY, {"callId": call_id, "limit": 100, "nextToken": token})
        page = (response or {}).get("getVideoKeyframes") or {}
        frames.extend(f for f in page.get("VideoKeyframes") or [] if isinstance(f, dict))
        token = page.get("nextToken")
        if not token:
            break
    return frames


def download_image(url: Optional[str], timeout: float = 10.0) -> Optional[bytes]:
    """The bytes behind a signed image link, or None when it cannot be read."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    try:
        # Only https links signed by getVideoKeyframes reach this call.
        with urllib.request.urlopen(url, timeout=timeout) as response:  # nosec B310
            body = response.read(MAX_IMAGE_BYTES + 1)
    except Exception as error:  # pylint: disable=broad-except
        logger.warning("Could not read a key frame image: %s", type(error).__name__)
        return None
    if len(body) > MAX_IMAGE_BYTES:
        logger.warning("Skipping a key frame image over %d bytes", MAX_IMAGE_BYTES)
        return None
    return body
