# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The decisions of the key frame text extractor, kept free of AWS.

See index.py for what the function does. Here: which events it acts on, where
a frame's image is, what the model is asked, and what of its answer is kept.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Optional, Tuple
from urllib.parse import unquote, urlparse

FRAME_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: What is kept of the text of one frame. A dense document page is about 2,000
#: characters; anything far longer is the model running on, not the slide.
MAX_TEXT_CHARS = 6000

#: What the model answers when the picture holds no readable text.
NO_TEXT = "NO TEXT"

#: What a frame shows, the first line of the model's answer. SCREEN: shared
#: slides, documents or applications. CAMERA: the meeting's video tiles, people.
#: MIXED: both, with shared content worth reading. An answer without a readable
#: line is taken as SCREEN, which is how every frame was treated before.
SCREEN, CAMERA, MIXED = "SCREEN", "CAMERA", "MIXED"
CONTENT_KINDS = (SCREEN, CAMERA, MIXED)

#: What is kept of a frame's title: the prompt asks for eight words at most.
MAX_TITLE_WORDS = 12
MAX_TITLE_CHARS = 100

PROMPT = (
    "This is a picture of a computer screen during a meeting. It may show content shared in the "
    "meeting (a slide, a document, a web page or an application), the meeting's camera video "
    "(tiles or a room camera showing people), or both.\n"
    "Answer in exactly this form:\n"
    "CONTENT: SCREEN, CAMERA or MIXED\n"
    "TITLE: a few words naming the main shared content\n"
    "then the text.\n"
    "CONTENT is SCREEN when the picture is mainly shared content, CAMERA when it is mainly camera "
    "video of people with no shared content worth reading, and MIXED when it shows both and the "
    "shared content is worth reading. TITLE names the main shared content in at most 8 words, "
    "for example TITLE: Q3 revenue review; for CAMERA write TITLE: none.\n"
    "Then transcribe the readable text of the main shared content only, top to bottom, one line "
    "per line of text, keeping headings, bullets and numbers as they are. Leave out the meeting's "
    "video tiles and their name labels, small terminal windows and status strips, the operating "
    "system's menu bar, taskbar and dock, and window title bars. If the shared content shows a "
    "chart, diagram or picture, add one line in square brackets saying what it shows, for example "
    "[Bar chart of revenue by quarter]. Do not describe or name people, faces or camera video. "
    "Write each line once; do not repeat text. If there is no readable shared text and nothing "
    f"else to describe, write exactly {NO_TEXT} after the two header lines."
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


#: A run of the same words or lines, one after another, this many times or more
#: is the model looping, not the slide: kept once. Slides do repeat a word or a
#: table cell a few times, so the bar is above that; units without a letter or
#: digit in them (table rules, "| --- |") are never collapsed.
MIN_LOOP_REPEATS = 4
#: The longest unit, in words, a loop is looked for in.
MAX_LOOP_WORDS = 12
#: The same, for whole lines; three identical lines in a row is already a loop.
MIN_LINE_REPEATS = 3
MAX_LOOP_LINES = 5
#: Text this long whose distinct words are fewer than this share of all its
#: words is a loop that varies too much to collapse; it is dropped. The
#: measured loop is 5 distinct words in hundreds; a page of templated rows
#: ("Item 1 covers budget, hiring and ...") is about 10%, and is kept.
MIN_WORDS_FOR_RATIO = 60
MIN_UNIQUE_WORD_RATIO = 0.08

_HEADER = re.compile(r"^[\s*_#>`-]*(CONTENT|TITLE)[\s*_]*[:=\-][\s*_]*(.*?)[\s*_]*$", re.IGNORECASE)
_KIND = re.compile(r"\b(SCREEN|CAMERA|MIXED)\b", re.IGNORECASE)
_NO_TITLE = {"", "none", "n/a", "na", "-", "no title", "(none)", "[none]"}
_FENCE = re.compile(r"^```[a-z]*\n?|\n?```$")
_WORD = re.compile(r"\w")


def _collapse_runs(units: list, min_repeats: int, max_length: int, key=lambda u: u) -> list:
    """``units`` with each run of a block repeated ``min_repeats`` times or more kept once."""
    keys = [key(unit) for unit in units]
    kept: list = []
    i = 0
    while i < len(units):
        step = 0
        for length in range(1, min(max_length, len(units) - i) + 1):
            block = keys[i : i + length]
            if not any(_WORD.search(k) for k in block):
                continue
            repeats = 1
            while keys[i + repeats * length : i + (repeats + 1) * length] == block:
                repeats += 1
            if repeats >= min_repeats:
                kept.extend(units[i : i + length])
                step = repeats * length
                break
        if not step:
            kept.append(units[i])
            step = 1
        i += step
    return kept


def collapse_repetition(text: str) -> str:
    """The text with a looping model's repeated words and lines kept once.

    Repeated lines are found on the lines with their spacing folded; repeated
    runs of words within each line after that.
    """
    lines = _collapse_runs(
        text.split("\n"), MIN_LINE_REPEATS, MAX_LOOP_LINES, key=lambda line: " ".join(line.split())
    )
    return "\n".join(_collapse_words(line) for line in lines)


def _collapse_words(line: str) -> str:
    """The line with a repeated run of words kept once; unchanged, spacing and
    all, when no run repeats -- a slide's code or aligned columns keep their layout."""
    words = line.split()
    if len(words) < MIN_LOOP_REPEATS:
        return line
    kept = _collapse_runs(words, MIN_LOOP_REPEATS, MAX_LOOP_WORDS)
    return line if len(kept) == len(words) else " ".join(kept)


def mostly_repeated(text: str) -> bool:
    """Whether the text is long and made of a few words said over and over."""
    words = re.findall(r"\w+", text.lower())
    if len(words) < MIN_WORDS_FOR_RATIO:
        return False
    return len(set(words)) / len(words) < MIN_UNIQUE_WORD_RATIO


def clean_text(answer: Any, truncated: bool = False) -> str:
    """What is stored as the frame's text: the model's answer, trimmed; "" for no text.

    A loop -- the same words or lines over and over, which a model reading a
    small, dense strip of text can fall into -- is kept once, and text that is
    still mostly repetition after that is dropped. ``truncated``: the model
    stopped at its token ceiling, so its last line is cut off and is left out.
    """
    if not isinstance(answer, str):
        return ""
    text = answer.strip()
    # Some models wrap their answer in a fence despite being asked not to.
    text = _FENCE.sub("", text).strip()
    if text.upper().rstrip(".") == NO_TEXT:
        return ""
    if truncated and "\n" in text:
        text = text.rsplit("\n", 1)[0].rstrip()
    text = collapse_repetition(text).strip()
    if mostly_repeated(text):
        return ""
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rsplit("\n", 1)[0]
    return text


def clean_title(value: str) -> str:
    """A TITLE line's value as stored: one line, unquoted, a few words; "" for none."""
    title = " ".join(value.split()).strip("\"'`*[]()")
    title = title.strip().rstrip(".")
    if title.lower() in _NO_TITLE:
        return ""
    words = title.split()
    if len(words) > MAX_TITLE_WORDS:
        title = " ".join(words[:MAX_TITLE_WORDS])
    return title[:MAX_TITLE_CHARS].strip()


def read_answer(answer: Any, truncated: bool = False) -> Tuple[str, str, str]:
    """ContentKind, Title and text from the model's answer.

    The header is read leniently (any case, Markdown emphasis), but only as a
    header: a CONTENT line naming one of the three kinds, then optionally a
    TITLE line. An answer that does not begin so -- a model that ignored the
    form, or a slide whose own first line is "Title: ..." -- is SCREEN with no
    title, and all of it is the text, as before the header was asked for. A
    CAMERA frame keeps no title and no text: what a model reads off a picture
    of people is name labels and the meeting's controls.
    """
    if not isinstance(answer, str):
        return SCREEN, "", ""
    lines = _FENCE.sub("", answer.strip()).strip().split("\n")
    kind, title = SCREEN, ""
    header = _HEADER.match(lines[0])
    found = (
        _KIND.search(header.group(2)) if header and header.group(1).upper() == "CONTENT" else None
    )
    if found:
        kind = found.group(1).upper()
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
        header = _HEADER.match(lines[0]) if lines else None
        if header and header.group(1).upper() == "TITLE":
            title = clean_title(header.group(2))
            lines.pop(0)
    if kind == CAMERA:
        return CAMERA, "", ""
    return kind, title, clean_text("\n".join(lines), truncated=truncated)


# ── Repeated frames ────────────────────────────────────────────────────────
#
# A shared application whose content keeps updating, or a page scrolled a
# little, gives frames that differ in pixels but show the same screen. Once a
# frame's text is read it is compared with the last frame before it that was
# not itself a repeat (and not a camera frame), and stored with ``DuplicateOf``
# naming that frame when the two show the same thing. The meeting page hides
# such frames by default and leaves them out of the exported deck.

#: Word-sequence similarity at or above which two frames show the same screen.
DUPLICATE_TEXT_SIMILARITY = 0.8
#: The lower similarity enough when the two frames' titles are the same. Not
#: the title alone: a model can name a whole deck rather than each slide.
DUPLICATE_TITLE_TEXT_SIMILARITY = 0.5

_WORDS = re.compile(r"\w+")


def _words(text: Any) -> list:
    return _WORDS.findall(text.casefold()) if isinstance(text, str) else []


def _lines(text: Any) -> list:
    if not isinstance(text, str):
        return []
    return [" ".join(_words(line)) for line in text.split("\n") if _words(line)]


def text_similarity(a: Any, b: Any) -> float:
    """How alike two frames' texts are, 0 to 1, by their words in order.

    1.0 for two empty texts (nothing on either to tell them apart), 0.0 when
    only one is empty.
    """
    wa, wb = _words(a), _words(b)
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def same_title(a: Any, b: Any) -> bool:
    """Whether two titles are the same, ignoring case, punctuation and spacing; never for none."""
    wa, wb = _words(a), _words(b)
    return bool(wa) and wa == wb


def extends(text: Any, earlier: Any) -> bool:
    """Whether ``text`` holds every line of ``earlier`` and more: a slide built up a point at a time.

    Such a frame is not a repeat however alike the two are: it is the fuller
    version, and hiding it would leave the last points out of the deck.
    """
    lines, before = _lines(text), _lines(earlier)
    return bool(before) and len(lines) > len(before) and set(before) <= set(lines)


def is_duplicate(title: str, text: str, earlier_title: Any, earlier_text: Any) -> bool:
    """Whether a frame shows the same screen as an earlier one, from what was read off both."""
    if not isinstance(earlier_text, str):
        return False  # the earlier frame is not read yet
    if extends(text, earlier_text):
        return False
    similarity = text_similarity(text, earlier_text)
    if text.strip() and earlier_text.strip() and similarity >= DUPLICATE_TEXT_SIMILARITY:
        return True
    return same_title(title, earlier_title) and similarity >= DUPLICATE_TITLE_TEXT_SIMILARITY


def _attr(item: Mapping[str, Any], name: str) -> Any:
    value = item.get(name)
    return value.get("S") if isinstance(value, Mapping) else None


def earlier_original(items: Iterable[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """Of the frames before one, newest first (DynamoDB items), the one to compare it with.

    The newest that shows shared content and is not itself a repeat; camera
    frames and repeats are passed over. None when there is none, or when a
    frame on the way to it has not been read yet, so whether it is a repeat or
    a camera frame is not known.
    """
    for item in items:
        if _attr(item, "SlideText") is None:
            return None
        if _attr(item, "ContentKind") == CAMERA or _attr(item, "DuplicateOf"):
            continue
        return item
    return None
