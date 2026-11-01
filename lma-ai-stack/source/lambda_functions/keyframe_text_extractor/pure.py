# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The decisions of the key frame text extractor, kept free of AWS.

See index.py for what the function does. Here: which events it acts on, where
a frame's image is, what the model is asked, and what of its answer is kept.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional
from urllib.parse import unquote, urlparse

FRAME_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: What is kept of the text of one frame. A dense document page is about 2,000
#: characters; anything far longer is the model running on, not the slide.
MAX_TEXT_CHARS = 6000

#: What the model answers when the picture holds no readable text.
NO_TEXT = "NO TEXT"

PROMPT = (
    "This is a picture of the screen shared in a meeting: a slide, a document or an application. "
    "Transcribe all of the readable text on it, top to bottom, one line per line of text, keeping "
    "headings, bullets and numbers as they are. If it shows a chart, diagram or picture, add one "
    "line in square brackets saying what it shows, for example [Bar chart of revenue by quarter]. "
    "Do not describe or name people, faces or camera video. Output only the text. If there is no "
    f"readable text and nothing else to describe, answer exactly: {NO_TEXT}"
)


def posixify_filename(filename: str) -> str:
    """The folder name producers use for a meeting; the same rule as meeting deletion."""
    name = re.sub(r"[^a-zA-Z0-9_.]", "_", filename)
    name = re.sub(r"^_+", "", name)
    return re.sub(r"_+$", "", name)


def object_key(url: Any, call_id: str, bucket: str, prefix: str) -> Optional[str]:
    """The key of a frame's image, when the URL is in the meeting's own key-frame folder.

    The same rule the getVideoKeyframes resolver applies before it signs a
    link: this deployment's bucket, the meeting's folder, nothing else.
    """
    if not isinstance(url, str) or not bucket:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if not parsed.hostname.startswith(f"{bucket}.s3."):
        return None
    key = unquote(parsed.path.lstrip("/"))
    if not key.startswith(f"{prefix}{posixify_filename(call_id)}/") or ".." in key:
        return None
    return key


def request_from(event: Mapping[str, Any]) -> Optional[Mapping[str, str]]:
    """CallId, FrameId and ImageUrl from an invocation, or None when one is unusable."""
    call_id = event.get("CallId")
    frame_id = event.get("FrameId")
    image_url = event.get("ImageUrl")
    if not isinstance(call_id, str) or not call_id:
        return None
    if not isinstance(frame_id, str) or not FRAME_ID.match(frame_id):
        return None
    if not isinstance(image_url, str):
        return None
    return {"CallId": call_id, "FrameId": frame_id, "ImageUrl": image_url}


def clean_text(answer: Any) -> str:
    """What is stored as the frame's text: the model's answer, trimmed; "" for no text."""
    if not isinstance(answer, str):
        return ""
    text = answer.strip()
    # Some models wrap their answer in a fence despite being asked not to.
    text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    if text.upper().rstrip(".") == NO_TEXT:
        return ""
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rsplit("\n", 1)[0]
    return text
