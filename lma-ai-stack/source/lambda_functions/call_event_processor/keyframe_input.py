# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The addVideoKeyframe input for an ADD_VIDEO_KEYFRAME event.

A key frame is one picture of the shared screen, sent by the Virtual Participant
(keyframe-sampler.ts) or by the WebSocket transcriber for the desktop capture app.
Both send the same fields; see docs/meeting-video.md.

Kept free of AWS imports, like pure.py, so the validation can be tested without
the Lambda layer on the path.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

KEYFRAME_SOURCES = frozenset({"VIRTUAL_PARTICIPANT", "DESKTOP_CAPTURE"})

# A frame id is a zero-padded millisecond position plus a short suffix (see
# frameIdFor in keyframe-sampler.ts); it becomes part of the item's sort key.
MAX_FRAME_ID_LENGTH = 64


class KeyframeEventError(TypeError):
    """An ADD_VIDEO_KEYFRAME event that cannot be stored."""


def _number(message: Mapping[str, Any], name: str, *, required: bool) -> Optional[float]:
    value = message.get(name)
    if value is None:
        if required:
            raise KeyframeEventError(f"ADD_VIDEO_KEYFRAME is missing {name}")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise KeyframeEventError(f"ADD_VIDEO_KEYFRAME {name} is not a number")
    if value < 0:
        raise KeyframeEventError(f"ADD_VIDEO_KEYFRAME {name} is negative")
    return float(value)


def _image_url(message: Mapping[str, Any], name: str) -> str:
    value = message.get(name)
    if not isinstance(value, str) or not value.startswith("https://"):
        raise KeyframeEventError(f"ADD_VIDEO_KEYFRAME {name} is not an https URL")
    return value


def build_keyframe_input(
    message: Mapping[str, Any],
    owner: Optional[str],
    shared_with: Any,
    expires_after: int,
) -> Dict[str, Any]:
    """The addVideoKeyframe input, or KeyframeEventError for an event that cannot be stored.

    Owner and SharedWith come from the meeting rather than from the event, so a
    key frame is visible to exactly the people who can see the meeting's
    transcript. ExpiresAfter is the transcript's retention, for the same reason.
    """
    call_id = message.get("CallId")
    if not isinstance(call_id, str) or not call_id:
        raise KeyframeEventError("ADD_VIDEO_KEYFRAME is missing CallId")
    frame_id = message.get("FrameId")
    if not isinstance(frame_id, str) or not frame_id or len(frame_id) > MAX_FRAME_ID_LENGTH:
        raise KeyframeEventError("ADD_VIDEO_KEYFRAME FrameId is missing or too long")
    source = message.get("Source")
    if source not in KEYFRAME_SOURCES:
        raise KeyframeEventError("ADD_VIDEO_KEYFRAME Source is not recognised")
    share_detected = message.get("ShareDetected")
    if share_detected is not None and not isinstance(share_detected, bool):
        raise KeyframeEventError("ADD_VIDEO_KEYFRAME ShareDetected is not a boolean")

    width = _number(message, "Width", required=False)
    height = _number(message, "Height", required=False)
    item: Dict[str, Any] = {
        "CallId": call_id,
        "FrameId": frame_id,
        "Timestamp": _number(message, "Timestamp", required=True),
        "VideoOffset": _number(message, "VideoOffset", required=False),
        "ImageUrl": _image_url(message, "ImageUrl"),
        "ThumbnailUrl": _image_url(message, "ThumbnailUrl"),
        "Width": int(width) if width is not None else None,
        "Height": int(height) if height is not None else None,
        "Source": source,
        "ShareDetected": share_detected,
        "ChangeScore": _number(message, "ChangeScore", required=False),
        "ExpiresAfter": expires_after,
        "Owner": owner,
        "SharedWith": shared_with,
    }
    created_at = message.get("CreatedAt")
    if isinstance(created_at, str) and created_at:
        item["CreatedAt"] = created_at
    # Unset optional fields are left out rather than written as nulls.
    return {k: v for k, v in item.items() if v is not None}
