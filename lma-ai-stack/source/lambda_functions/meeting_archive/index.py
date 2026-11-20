#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""A meeting's files as one ZIP archive, built in the background.

Three entry points share this function:

* ``Mutation.prepareMeetingArchive(CallId, Contents)`` -- the AppSync resolver.
  It answers the callers ``getCall`` answers (an Admin, the meeting's owner,
  somebody it is shared with, or a backend role signing with IAM), records the
  request as a status object, invokes this function again asynchronously to
  build the archive, and returns at once: an AppSync resolver has 30 seconds.
* ``Query.getMeetingArchive(CallId, ArchiveId)`` -- the same access rule, plus
  the archive must be one the caller asked for (an Admin may read any). Returns
  the status, and once the archive is ready a GET link signed for
  :data:`LINK_SECONDS`.
* ``{"Trigger": "build", "CallId", "ArchiveId"}`` with no identity -- the
  second leg, invoked only by this function, which builds the archive.

``Contents`` is EVERYTHING (the summary as HTML and Markdown, the transcript as
text and HTML unless it was deleted under the Retention settings, every audio
and video recording, the key frames, the chat attachments, the meeting's
details as JSON, the consent record and its evidence for the meeting's owner
or an Admin, and README.md listing every file and anything left out and why),
KEY_FRAMES or CHAT_ATTACHMENTS (those files and README.md).

The archive is streamed: each object is read from S3 in chunks of
:data:`READ_CHUNK_BYTES` into :mod:`zipfile`, which writes to
:class:`MultipartWriter`, an S3 multipart upload holding at most one part
(:data:`PART_BYTES`) in memory. ``zipfile`` writes a non-seekable stream with
data descriptors and, for the recordings, ZIP64 records, so neither the
archive nor any file in it is ever held whole in memory or on disk. An archive
whose files come to more than :data:`MAX_ARCHIVE_BYTES` is refused with a
message saying so.

Archives and their status objects are written under :data:`ARCHIVE_PREFIX`,
which the recordings bucket's lifecycle rule deletes after a day. Every object
read is the meeting's own: recordings named by the meeting record, and key
frames, attachments and consent evidence inside the meeting's folders. See
docs/web-ui-guide.md and docs/privacy-and-data-handling.md.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import uuid
import zipfile
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Tuple
from urllib.parse import unquote, urlparse

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import ClientError

# Stdlib logging rather than aws_lambda_powertools: this function carries no
# layer and uses the runtime's own boto3.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
REGION = os.environ.get("AWS_REGION", "us-east-1")
EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "lma-meeting-archives/")
AUDIO_PREFIX = os.environ.get("AUDIO_PREFIX", "lma-audio-recordings/")
VIDEO_PREFIX = os.environ.get("VIDEO_PREFIX", "lma-video-recordings/")
KEYFRAMES_PREFIX = os.environ.get("KEYFRAMES_PREFIX", "lma-video-keyframes/")
ATTACHMENTS_PREFIX = os.environ.get("ATTACHMENTS_PREFIX", "lma-chat-attachments/")
CONSENT_EVIDENCE_PREFIX = os.environ.get("CONSENT_EVIDENCE_PREFIX", "lma-consent-evidence/")
LINK_SECONDS = int(os.environ.get("LINK_SECONDS", "3600"))
#: The most an archive's files may come to (5 GiB by default).
MAX_ARCHIVE_BYTES = int(os.environ.get("MAX_ARCHIVE_BYTES", str(5 * 1024**3)))
#: The size of each multipart upload part, and so the most of the archive held in memory.
PART_BYTES = int(os.environ.get("PART_BYTES", str(16 * 1024**2)))
#: How much of an object is read from S3 at a time.
READ_CHUNK_BYTES = 1024**2
#: A build still preparing after this long has stopped (the function's timeout is 15 minutes).
STALE_SECONDS = 16 * 60

CONTENTS = ("EVERYTHING", "KEY_FRAMES", "CHAT_ATTACHMENTS")
PREPARING, READY, FAILED = "PREPARING", "READY", "FAILED"
ARCHIVE_ID = re.compile(r"^[0-9a-f]{32}$")
MEDIA_EXTENSIONS = (
    ".wav",
    ".mp3",
    ".m4a",
    ".ogg",
    ".webm",
    ".mp4",
    ".mkv",
    ".jpg",
    ".jpeg",
    ".png",
)

DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)
LAMBDA = boto3.client("lambda", config=CLIENT_CONFIG)
DESERIALIZER = TypeDeserializer()


def _s3_client(addressing_style: Literal["virtual", "path"]) -> Any:
    """SigV4 links on the deployment's regional endpoint, in its partition."""
    return boto3.client(
        "s3",
        region_name=REGION,
        config=BotoCoreConfig(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style, "us_east_1_regional_endpoint": "regional"},
            retries={"mode": "adaptive", "max_attempts": 3},
        ),
    )


# Virtual-hosted links, except for a bucket whose name has dots, which does not
# match S3's TLS certificate as a host name.
S3 = _s3_client("path" if "." in RECORDINGS_BUCKET else "virtual")


class ForbiddenError(Exception):
    """The caller may not see the meeting, or the archive."""


class BadRequestError(Exception):
    """An argument that cannot be used."""


class ArchiveTooLargeError(Exception):
    """The archive's files come to more than MAX_ARCHIVE_BYTES."""


def posixify_filename(filename: str) -> str:
    """The folder name producers use for a meeting; the same rule as meeting deletion."""
    name = re.sub(r"[^a-zA-Z0-9_.]", "_", filename)
    name = re.sub(r"^_+", "", name)
    return re.sub(r"_+$", "", name)


def key_in_folder(key: Any, folder: str) -> bool:
    """True when ``key`` names an object inside ``folder``, a prefix ending in "/".

    The rule is applied to the key's "/"-separated segments: every segment must
    be non-empty and neither "." nor "..". A meeting's folder name is its CallId
    made safe as a file name, which keeps dots, so a run of dots inside a
    segment is part of a name and is accepted. The same helper is copied into
    each function that checks such a key; lma-ai-stack/test/
    test_key_in_folder_copies_agree.py keeps the copies identical.
    """
    if not isinstance(key, str) or not folder.endswith("/") or not key.startswith(folder):
        return False
    return all(segment not in ("", ".", "..") for segment in key.split("/"))


def caller_of(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Who is asking, from the AppSync ``identity`` block only.

    The rule ``getCall``'s resolver applies: a request signed with IAM by one of
    LMA's own roles carries an assumed-role ``userArn`` and no user-pool
    claims, and is the backend.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    user_arn = str(identity.get("userArn") or "")
    return {
        "username": str(identity.get("username") or ""),
        "subject": claims.get("sub") or identity.get("sub") or "",
        "is_admin": ADMIN_GROUP in groups,
        "is_backend": not claims and ":assumed-role/" in user_arn,
    }


# BEGIN shared_with_entries -- one text in every Lambda that reads SharedWith;
# lma-ai-stack/test/test_shared_with_access.py holds the copies to it.
# The entries of a SharedWith value, read as the AppSync resolvers read them
# (the "SharedWith entries" VTL block and sharedWithEntries in the JS
# resolvers): a list, tuple or set holds one entry per string element, and a
# string -- comma-separated or "[a, b]" -- is split at every "[", "]" and ",".
# Those three characters only ever separate entries, so a list element that
# contains one is not an entry. Entries are trimmed and empty ones dropped;
# membership is then a comparison of whole entries.
def shared_with_entries(value: object) -> list:
    """The trimmed, non-empty entries of a SharedWith value."""
    if isinstance(value, str):
        parts = value.replace("[", ",").replace("]", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = [
            part
            for part in value
            if isinstance(part, str) and not any(separator in part for separator in "[],")
        ]
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


# END shared_with_entries


def _plain(value: Any) -> Any:
    """A deserialized DynamoDB value with its Decimals as int or float, for JSON."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, (bytes, bytearray)):
        return None
    return value


def _item(raw: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: _plain(DESERIALIZER.deserialize(v)) for k, v in raw.items()}


def read_meeting(call_id: str) -> Optional[Dict[str, Any]]:
    response = DYNAMODB.get_item(
        TableName=EVENT_SOURCING_TABLE,
        Key={"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}},
    )
    raw = response.get("Item")
    return _item(raw) if raw else None


def may_read(caller: Mapping[str, Any], meeting: Optional[Mapping[str, Any]]) -> bool:
    """The ``getCall`` rule: Admin, backend, owner, or somebody it is shared with."""
    if meeting is None:
        return False
    if caller["is_admin"] or caller["is_backend"]:
        return True
    username = caller["username"]
    owner = str(meeting.get("Owner") or "")
    return bool(username) and (
        owner == username or username in shared_with_entries(meeting.get("SharedWith"))
    )


def may_read_consent(caller: Mapping[str, Any], meeting: Mapping[str, Any]) -> bool:
    """The consent record and its evidence go only to the meeting's owner or an Admin."""
    if caller["is_admin"]:
        return True
    username = caller["username"]
    return bool(username) and str(meeting.get("Owner") or "") == username


def require_meeting(caller: Mapping[str, Any], call_id: str) -> Dict[str, Any]:
    meeting = read_meeting(call_id)
    if not may_read(caller, meeting):
        # The same answer for a meeting that does not exist as for one the
        # caller may not see, so the field cannot be used to test which exist.
        LOGGER.info("refused an archive of %s to %s", call_id, caller["subject"] or "(none)")
        raise ForbiddenError("Unauthorized: you do not have access to this meeting")
    return meeting  # type: ignore[return-value]


# --- status objects ----------------------------------------------------------


def archive_folder(call_id: str, archive_id: str) -> str:
    return f"{ARCHIVE_PREFIX}{posixify_filename(call_id)}/{archive_id}/"


def status_key(call_id: str, archive_id: str) -> str:
    return f"{archive_folder(call_id, archive_id)}status.json"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_status(status: Mapping[str, Any]) -> None:
    S3.put_object(
        Bucket=RECORDINGS_BUCKET,
        Key=status_key(status["CallId"], status["ArchiveId"]),
        Body=json.dumps(status).encode("utf-8"),
        ContentType="application/json",
    )


def read_status(call_id: str, archive_id: str) -> Optional[Dict[str, Any]]:
    """The archive's status object, or None when there is none."""
    try:
        body = S3.get_object(Bucket=RECORDINGS_BUCKET, Key=status_key(call_id, archive_id))["Body"]
    except S3.exceptions.NoSuchKey:
        return None
    except ClientError as error:
        # A missing key reads as NoSuchKey with s3:ListBucket on the prefix,
        # and as AccessDenied without it; either way there is no archive.
        if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "AccessDenied"):
            return None
        raise
    return json.loads(body.read())


#: How long an archive already prepared is offered again instead of a new one:
#: well inside the day the lifecycle rule keeps it.
REUSE_SECONDS = 20 * 3600


def _instant(stamp: Any) -> Optional[dt.datetime]:
    """An ISO 8601 UTC time ("...Z", with or without fractions of a second), or None."""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def built_after_last_change(found: Mapping[str, Any], meeting: Mapping[str, Any]) -> bool:
    """Whether an archive was started after the meeting record last changed, and
    after its transcript was deleted, so that it still holds what a new archive
    would. A meeting still going on changes with each update, so an archive of
    it is not offered again once the meeting has moved on."""
    started = _instant(found.get("CreatedAt"))
    if started is None:
        return False
    for attribute in ("UpdatedAt", "TranscriptDeletedAt"):
        changed = _instant(meeting.get(attribute))
        if changed is not None and changed >= started:
            return False
    return True


def reusable_archive(
    call_id: str, status: Mapping[str, Any], meeting: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """The caller's own archive of the same contents still preparing, or ready,
    recent and started after the meeting last changed, so that asking again
    neither starts a second build nor waits, and never answers with an archive
    older than the meeting it copies."""
    folder = f"{ARCHIVE_PREFIX}{posixify_filename(call_id)}/"
    best: Optional[Dict[str, Any]] = None
    for page in S3.get_paginator("list_objects_v2").paginate(
        Bucket=RECORDINGS_BUCKET, Prefix=folder
    ):
        for entry in page.get("Contents", []):
            parts = entry["Key"][len(folder) :].split("/")
            if len(parts) != 2 or parts[1] != "status.json" or not ARCHIVE_ID.match(parts[0]):
                continue
            found = read_status(call_id, parts[0])
            if not found or any(
                found.get(k) != status.get(k) for k in ("Contents", "RequestedBy", "IncludeConsent")
            ):
                continue
            age = _age_seconds(found.get("CreatedAt"))
            fresh = built_after_last_change(found, meeting) and (
                (found.get("Status") == PREPARING and age <= STALE_SECONDS)
                or (found.get("Status") == READY and age <= REUSE_SECONDS)
            )
            if fresh and (best is None or str(found.get("CreatedAt")) > str(best.get("CreatedAt"))):
                best = found
    return best


def archive_file_name(call_id: str, contents: str) -> str:
    suffix = {
        "EVERYTHING": "",
        "KEY_FRAMES": "-key-frames",
        "CHAT_ATTACHMENTS": "-chat-attachments",
    }
    return f"Meeting-{posixify_filename(call_id) or 'meeting'}{suffix[contents]}.zip"


# --- what goes in --------------------------------------------------------------


def recording_key(url: Any, prefix: str) -> Optional[str]:
    """The key a stored recording URL names, when it is in this bucket directly under ``prefix``.

    The producers store ``https://<bucket>.s3.<region>.<suffix>/<key>``, with the
    key URL-encoded by the Virtual Participant and as-is by the transcriber, as
    meeting deletion reads it.
    """
    if not url or not isinstance(url, str) or not RECORDINGS_BUCKET:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").startswith(
        f"{RECORDINGS_BUCKET}.s3."
    ):
        return None
    key = unquote(parsed.path.lstrip("/"))
    name = key[len(prefix) :]
    if not key.startswith(prefix) or not name or "/" in name or name in (".", ".."):
        return None
    return key


def session_urls(meeting: Mapping[str, Any], single: str, many: str) -> List[str]:
    """Every session's recording URL, each once: the ``many`` list (RecordingUrls,
    VideoRecordingUrls), which a meeting the Virtual Participant joined again
    keeps and which can name one twice, then the current ``single`` one, as
    meeting deletion reads them."""
    listed = meeting.get(many)
    urls: List[str] = []
    for url in [*(listed if isinstance(listed, list) else []), meeting.get(single)]:
        if isinstance(url, str) and url and url not in urls:
            urls.append(url)
    return urls


def query_items(partition: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        "TableName": EVENT_SOURCING_TABLE,
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": partition}},
    }
    while True:
        page = DYNAMODB.query(**kwargs)
        items.extend(_item(raw) for raw in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def object_size(key: str) -> Optional[int]:
    """The object's size, or None when it is not there."""
    try:
        return int(S3.head_object(Bucket=RECORDINGS_BUCKET, Key=key)["ContentLength"])
    except Exception:  # noqa: BLE001 - a missing or unreadable object is left out and listed
        return None


def list_folder(folder: str) -> List[Tuple[str, int]]:
    found: List[Tuple[str, int]] = []
    for page in S3.get_paginator("list_objects_v2").paginate(
        Bucket=RECORDINGS_BUCKET, Prefix=folder
    ):
        for entry in page.get("Contents", []):
            if key_in_folder(entry["Key"], folder):
                found.append((entry["Key"], int(entry["Size"])))
    return found


def safe_name(name: Any, fallback: str) -> str:
    """A name for a file in the archive: no folders, no control characters."""
    text = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", "_", str(name or "")).strip(" .")
    return text[:120] or fallback


def extension_of(key: str, default: str = "") -> str:
    match = re.search(r"(\.[A-Za-z0-9]{1,5})$", key)
    return match.group(1).lower() if match else default


class Plan:
    """The archive's files, in order, and what was left out and why."""

    def __init__(self) -> None:
        self.files: List[Dict[str, Any]] = []
        self.left_out: List[Tuple[str, str]] = []
        self.paths: set = set()

    def _unique(self, path: str) -> str:
        if path not in self.paths:
            self.paths.add(path)
            return path
        stem, dot, ext = path.rpartition(".")
        if not dot:
            stem, ext = path, ""
        n = 2
        while True:
            candidate = f"{stem}-{n}.{ext}" if dot else f"{stem}-{n}"
            if candidate not in self.paths:
                self.paths.add(candidate)
                return candidate
            n += 1

    def add_text(self, path: str, text: str, description: str) -> None:
        data = text.encode("utf-8")
        self.files.append(
            {"path": self._unique(path), "data": data, "size": len(data), "about": description}
        )

    def add_object(self, path: str, key: str, size: int, description: str) -> str:
        unique = self._unique(path)
        self.files.append({"path": unique, "key": key, "size": size, "about": description})
        return unique

    def leave_out(self, what: str, why: str) -> None:
        self.left_out.append((what, why))

    @property
    def total(self) -> int:
        return sum(f["size"] for f in self.files)


# --- the summary and transcript, rendered here ------------------------------------

#: The order the meeting page and every summary download use (summary.js,
#: SUMMARY_SECTION_ORDER); custom sections follow in their stored order.
SECTION_ORDER = [
    ("SUMMARY", "MEETING SUMMARY", "EXECUTIVE SUMMARY"),
    ("ACTIONS", "ACTION", "ACTION ITEMS", "ACTION ITEM"),
    ("AGENDA COVERAGE", "AGENDA"),
    ("DETAILS", "DETAIL", "MEETING DETAILS", "KEY DETAILS"),
    ("CHAPTERS", "CHAPTER", "MEETING CHAPTERS"),
    (
        "SCREEN SHARE SUMMARY",
        "SCREEN SHARE",
        "SCREENSHARE SUMMARY",
        "SCREENSHARE",
        "SHARED SCREEN",
        "SHARED SCREEN SUMMARY",
        "SLIDES PRESENTED",
        "SLIDES",
    ),
    (
        "CHAT AND SHARED LINKS",
        "CHAT SHARED LINKS",
        "CHAT AND LINKS",
        "CHAT",
        "CHAT SUMMARY",
        "SHARED LINKS",
    ),
]


def section_key(title: Any) -> str:
    text = str(title or "").upper().replace("&", " AND ")
    return re.sub(r"[^0-9A-Z]+", " ", text).strip()


SECTION_RANKS = {
    section_key(alias): rank for rank, names in enumerate(SECTION_ORDER) for alias in names
}


def summary_sections(text: Any) -> List[Tuple[str, str]]:
    """The summary's sections in reading order: [(title, markdown)]."""
    if not isinstance(text, str) or not text.strip():
        return []
    sections: List[Tuple[str, str]]
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        sections = [(str(k), str(v if v is not None else "")) for k, v in parsed.items()]
    else:
        # A summary written by hand: one section, as written.
        sections = [("Summary", text)]
    ranked = sorted(
        enumerate(sections),
        key=lambda pair: (SECTION_RANKS.get(section_key(pair[1][0]), len(SECTION_ORDER)), pair[0]),
    )
    return [section for _index, section in ranked]


KEYFRAME_REFERENCE = re.compile(r"!\[([^\]\n]*)\]\(keyframe:([A-Za-z0-9_-]{1,64})\)")
SAFE_LINK = re.compile(r"^(?:https?://|mailto:)[^\s\"'<>]+$", re.IGNORECASE)
INLINE_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")


def summary_markdown(
    title: str, facts: List[Tuple[str, str]], sections, frames: Mapping[str, str]
) -> str:
    """The summary as Markdown; a key frame in the archive is a picture of its file."""

    def frame(match: re.Match) -> str:
        alt, frame_id = match.group(1), match.group(2)
        path = frames.get(frame_id)
        return f"![{alt}](../{path})" if path else f"({alt})"

    parts = [f"# {title}", "\n".join(f"- **{label}:** {value}" for label, value in facts)]
    if not sections:
        parts.append("## Summary\n\nNot available")
    for name, body in sections:
        parts.append(f"## {name}\n\n{KEYFRAME_REFERENCE.sub(frame, body).strip()}")
    return "\n\n".join(parts) + "\n"


def _inline_html(line: str, frames: Mapping[str, str]) -> str:
    """One line of Markdown as HTML: every character escaped, then bold, code,
    http(s) and mailto links, and key frames as links to their file in the archive."""
    pieces: List[str] = []
    position = 0
    pattern = re.compile(f"{KEYFRAME_REFERENCE.pattern}|{INLINE_LINK.pattern}")
    for match in pattern.finditer(line):
        pieces.append(_emphasis(html.escape(line[position : match.start()])))
        if match.group(2):
            path = frames.get(match.group(2))
            alt = html.escape(match.group(1) or "Shared screen")
            pieces.append(f'<a href="../{html.escape(path)}">{alt}</a>' if path else f"({alt})")
        else:
            label, href = match.group(3), match.group(4)
            if SAFE_LINK.match(href):
                pieces.append(
                    f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer" target="_blank">'
                    f"{_emphasis(html.escape(label or href))}</a>"
                )
            else:
                pieces.append(_emphasis(html.escape(label or href)))
        position = match.end()
    pieces.append(_emphasis(html.escape(line[position:])))
    return "".join(pieces)


def _emphasis(escaped: str) -> str:
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)


def markdown_html(markdown: str, frames: Mapping[str, str]) -> str:
    """Markdown as escaped HTML: headings, bullet and numbered lists, paragraphs."""
    out: List[str] = []
    open_list: Optional[str] = None
    for raw in str(markdown or "").splitlines():
        line = raw.rstrip()
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        number = re.match(r"^\s*\d{1,3}[.)]\s+(.*)$", line)
        kind = "ul" if bullet else "ol" if number else None
        if open_list and kind != open_list:
            out.append(f"</{open_list}>")
            open_list = None
        if kind:
            if not open_list:
                out.append(f"<{kind}>")
                open_list = kind
            out.append(f"<li>{_inline_html((bullet or number).group(1), frames)}</li>")  # type: ignore[union-attr]
            continue
        heading = re.match(r"^\s*(#{1,6})\s+(.*?)\s*#*$", line)
        if heading:
            level = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline_html(heading.group(2), frames)}</h{level}>")
        elif line.strip():
            out.append(f"<p>{_inline_html(line.strip(), frames)}</p>")
    if open_list:
        out.append(f"</{open_list}>")
    return "\n".join(out)


PAGE_CSS = (
    "body{font:15px/1.55 -apple-system,'Segoe UI',Roboto,Arial,sans-serif;max-width:52rem;margin:2rem auto;"
    "padding:0 1.5rem;color:#16191f}dl{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem}"
    "dt{font-weight:600}dd{margin:0}section{border-top:1px solid #ddd;padding-top:.8rem}"
    ".segment{white-space:pre-wrap;margin:0 0 .4rem}.time{color:#5f6b7a}"
)
PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


def html_page(title: str, facts: List[Tuple[str, str]], body: str) -> str:
    rows = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>" for k, v in facts)
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{PAGE_CSP}">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body>"
        f"<h1>{html.escape(title)}</h1><dl>{rows}</dl>{body}</body></html>\n"
    )


def summary_html(title: str, facts, sections, frames: Mapping[str, str]) -> str:
    body = (
        "".join(
            f"<section><h2>{html.escape(name)}</h2>{markdown_html(text, frames)}</section>"
            for name, text in sections
        )
        or "<section><h2>Summary</h2><p>Not available</p></section>"
    )
    return html_page(title, facts, body)


def clock(seconds: Any) -> str:
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        secs = 0.0
    secs = max(secs, 0.0)
    hours, rest = divmod(secs, 3600)
    minutes, sec = divmod(rest, 60)
    return (
        f"{int(hours)}:{int(minutes):02d}:{sec:04.1f}"
        if hours
        else f"{int(minutes):02d}:{sec:04.1f}"
    )


def transcript_rows(segments: Iterable[Mapping[str, Any]]) -> List[Tuple[str, str, str]]:
    """Final segments in time order, as (speaker, start, text)."""
    final = [
        s for s in segments if not s.get("IsPartial") and str(s.get("Transcript") or "").strip()
    ]
    final.sort(key=lambda s: (float(s.get("EndTime") or 0), float(s.get("StartTime") or 0)))
    rows = []
    for segment in final:
        channel = str(segment.get("Channel") or "")
        speaker = str(segment.get("Speaker") or channel or "Unknown").strip()
        if channel == "CHAT":
            speaker = f"{speaker} (chat)"
        elif channel in ("AGENT_ASSISTANT", "MEETING_ASSISTANT", "CHAT_ASSISTANT"):
            speaker = "MEETING ASSISTANT"
        rows.append(
            (speaker, clock(segment.get("StartTime")), str(segment.get("Transcript")).strip())
        )
    return rows


def transcript_text(rows) -> str:
    return "\n".join(f"{speaker} [{start}]: {text}" for speaker, start, text in rows) + "\n"


def transcript_html(title: str, facts, rows) -> str:
    body = "".join(
        f'<p class="segment"><strong>{html.escape(s)}</strong> <span class="time">[{html.escape(t)}]</span>: '
        f"{html.escape(x)}</p>"
        for s, t, x in rows
    )
    return html_page(title, facts, f"<section>{body}</section>")


# --- the plan ----------------------------------------------------------------------

#: The meeting record attributes meeting.json holds: fields of the schema's Call
#: type that getCall answers (lma-ai-stack/test/test_meeting_archive_template.py
#: holds the list to it), with the session lists of a meeting joined again. The
#: keys, the summary (in summary/) and the consent record (in consent/, for the
#: owner and Admins) are not among them, nor is any attribute added later.
DETAILS_FIELDS = (
    "CallId",
    "CreatedAt",
    "UpdatedAt",
    "Status",
    "Owner",
    "SharedWith",
    "AgentId",
    "CustomerPhoneNumber",
    "SystemPhoneNumber",
    "TotalConversationDurationMillis",
    "RecordingUrl",
    "VideoRecordingUrl",
    "RecordingUrls",
    "VideoRecordingUrls",
    "Agenda",
    "AgendaSource",
    "AgendaUpdatedBy",
    "AgendaUpdatedAt",
    "SpeakerIdentities",
    "CaptureSource",
    "TranscriptionEngine",
    "TranscriptionLanguage",
    "SpeakerSeparation",
    "DetectedLanguages",
    "TranscriptDeletionScheduledFor",
    "TranscriptDeletedAt",
    "TranscriptSegmentsDeleted",
    "TranscriptFilesDeleted",
)


def meeting_title(call_id: str) -> str:
    """The meeting's name, without the start time producers append to its CallId."""
    return re.sub(r"-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(-\d{3})?$", "", call_id) or call_id


def plan_archive(
    call_id: str, meeting: Mapping[str, Any], contents: str, include_consent: bool
) -> Plan:
    plan = Plan()
    folder = posixify_filename(call_id)
    everything = contents == "EVERYTHING"

    # Key frames: every frame image of the meeting, in its own folder.
    frames: Dict[str, str] = {}
    if everything or contents == "KEY_FRAMES":
        frames_folder = f"{KEYFRAMES_PREFIX}{folder}/"
        items = query_items(f"kf#{call_id}")
        for item in sorted(items, key=lambda i: str(i.get("SK") or "")):
            frame_id = str(item.get("SK") or "")[2:]
            key = recording_key_in_folder(item.get("ImageUrl"), frames_folder)
            size = object_size(key) if key else None
            if key is None or size is None:
                plan.leave_out(f"Key frame {frame_id}", "its picture is no longer stored")
                continue
            frames[frame_id] = plan.add_object(
                f"keyframes/{safe_name(frame_id, 'frame')}{extension_of(key, '.jpg')}",
                key,
                size,
                "Key frame",
            )
        if not items:
            plan.leave_out("Key frames", "nothing was shared on screen, or the frames were removed")
        if everything:
            plan.leave_out(
                "Slides (PDF)",
                "the slide deck is printed by the browser; use Download > Screen share > Slides on the meeting page",
            )

    if contents in ("EVERYTHING", "CHAT_ATTACHMENTS"):
        attachments_folder = f"{ATTACHMENTS_PREFIX}{folder}/"
        items = query_items(f"att#{call_id}")
        for item in items:
            name = safe_name(item.get("FileName"), "attachment")
            key = item.get("S3Key")
            if item.get("Status") != "STORED" or not key_in_folder(key, attachments_folder):
                plan.leave_out(f"Chat attachment {name}", "it was not stored")
                continue
            size = object_size(key)
            if size is None:
                plan.leave_out(f"Chat attachment {name}", "it is no longer stored")
                continue
            attachment_id = safe_name(str(item.get("SK") or "")[2:], "attachment")
            plan.add_object(
                f"chat-attachments/{attachment_id}-{name}", key, size, "Chat attachment"
            )
        if not items:
            plan.leave_out("Chat attachments", "none were captured for this meeting")

    if not everything:
        return plan

    title = meeting_title(call_id)
    segments = [] if meeting.get("TranscriptDeletedAt") else query_items(f"trs#{call_id}")
    rows = transcript_rows(segments)
    participants = sorted(
        {s for s, _t, _x in rows if not s.endswith("(chat)") and s != "MEETING ASSISTANT"}
    )
    facts = [("Meeting ID", call_id)]
    for label, attribute in (("Date", "CreatedAt"), ("Owner", "Owner")):
        if meeting.get(attribute):
            facts.append((label, str(meeting[attribute])))
    if participants:
        facts.append(("Participants", ", ".join(participants)))
    if meeting.get("TotalConversationDurationMillis"):
        facts.append(("Duration", clock(float(meeting["TotalConversationDurationMillis"]) / 1000)))

    sections = summary_sections(meeting.get("CallSummaryText"))
    plan.add_text(
        "summary/summary.html", summary_html(title, facts, sections, frames), "Summary (web page)"
    )
    plan.add_text(
        "summary/summary.md", summary_markdown(title, facts, sections, frames), "Summary (Markdown)"
    )
    plan.leave_out(
        "Summary (Word, PDF)", "built in the browser; use Download > Summary on the meeting page"
    )

    if meeting.get("TranscriptDeletedAt"):
        plan.leave_out(
            "Transcript",
            f"deleted under the Retention settings on {meeting['TranscriptDeletedAt']}",
        )
    elif rows:
        plan.add_text("transcript/transcript.txt", transcript_text(rows), "Transcript (text)")
        plan.add_text(
            "transcript/transcript.html",
            transcript_html(f"Transcript: {title}", facts, rows),
            "Transcript (web page)",
        )
    else:
        plan.leave_out("Transcript", "the meeting has no transcript")

    sessions = []
    for kind, single, many, prefix in (
        ("audio", "RecordingUrl", "RecordingUrls", AUDIO_PREFIX),
        ("video", "VideoRecordingUrl", "VideoRecordingUrls", VIDEO_PREFIX),
    ):
        urls = session_urls(meeting, single, many)
        for number, url in enumerate(urls, start=1):
            key = recording_key(url, prefix)
            size = object_size(key) if key else None
            label = f"{kind.capitalize()} recording, session {number}"
            if key is None or size is None:
                plan.leave_out(
                    label,
                    "the file is no longer stored (removed under the Retention settings, or never written)",
                )
                continue
            path = plan.add_object(
                f"recordings/{kind}-session-{number}{extension_of(key)}", key, size, label
            )
            sessions.append({"Kind": kind, "Session": number, "File": path})
        if not urls:
            plan.leave_out(f"{kind.capitalize()} recording", "the meeting has none")

    details = {k: meeting[k] for k in DETAILS_FIELDS if k in meeting}
    details["Participants"] = participants
    details["Sessions"] = sessions
    plan.add_text(
        "meeting.json",
        json.dumps(details, indent=2, sort_keys=True, default=str) + "\n",
        "Meeting details",
    )

    if include_consent:
        if meeting.get("Consent") is not None:
            plan.add_text(
                "consent/consent.json",
                json.dumps(meeting.get("Consent"), indent=2, sort_keys=True, default=str) + "\n",
                "Consent record",
            )
            for key, size in list_folder(f"{CONSENT_EVIDENCE_PREFIX}{folder}/"):
                plan.add_object(
                    f"consent/evidence/{safe_name(key.rsplit('/', 1)[-1], 'evidence')}",
                    key,
                    size,
                    "Consent evidence",
                )
        else:
            plan.leave_out("Consent record", "the meeting has none")
    else:
        plan.leave_out(
            "Consent record and evidence", "included only for the meeting's owner and Admins"
        )
    return plan


def recording_key_in_folder(url: Any, folder: str) -> Optional[str]:
    """The key a stored image URL names, when it is in this bucket inside ``folder``."""
    if not isinstance(url, str) or not RECORDINGS_BUCKET:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").startswith(
        f"{RECORDINGS_BUCKET}.s3."
    ):
        return None
    key = unquote(parsed.path.lstrip("/"))
    return key if key_in_folder(key, folder) else None


def readme(call_id: str, contents: str, plan: Plan, include_consent: bool) -> str:
    lines = [
        f"# {meeting_title(call_id)}",
        "",
        f"Files from the meeting `{call_id}` in Live Meeting Assistant, prepared {now_iso()}"
        f" ({contents.replace('_', ' ').lower()}).",
        "",
        "This archive is a copy: the meeting's own files are kept, and removed, under the",
        "deployment's Retention settings. The copy LMA prepared for download is deleted after a day.",
        "",
        "## Files",
        "",
        "| File | What it is | Size (bytes) |",
        "|---|---|---|",
    ]
    lines += [f"| `{f['path']}` | {f['about']} | {f['size']} |" for f in plan.files]
    lines += ["| `README.md` | This list | - |", ""]
    if plan.left_out:
        lines += ["## Left out", ""]
        lines += [f"- **{what}**: {why}." for what, why in plan.left_out]
        lines.append("")
    if contents == "EVERYTHING":
        lines += [
            "Speaker names in the transcript are as recorded with each line; names given on the",
            "meeting page's Speakers panel are applied by the page's own downloads.",
            "",
        ]
    return "\n".join(lines)


# --- streaming ---------------------------------------------------------------------


class MultipartWriter:
    """A write-only, non-seekable stream that is an S3 multipart upload.

    At most one part (``part_bytes``) is held at a time; each full part is
    uploaded as it fills. ``limit`` is the most the archive may come to.
    """

    def __init__(
        self,
        s3: Any,
        bucket: str,
        key: str,
        part_bytes: Optional[int] = None,
        limit: Optional[int] = None,
    ):
        self.s3, self.bucket, self.key = s3, bucket, key
        self.part_bytes = part_bytes or PART_BYTES
        self.limit = limit or MAX_ARCHIVE_BYTES
        self.buffer = bytearray()
        self.parts: List[Dict[str, Any]] = []
        self.written = 0
        self.most_buffered = 0
        self.upload_id = s3.create_multipart_upload(
            Bucket=bucket, Key=key, ContentType="application/zip"
        )["UploadId"]

    def write(self, data: bytes) -> int:
        self.written += len(data)
        if self.written > self.limit:
            raise ArchiveTooLargeError(
                f"The archive would be larger than the {self.limit / 1024**3:.0f} GB an archive can hold."
            )
        self.buffer += data
        self.most_buffered = max(self.most_buffered, len(self.buffer))
        while len(self.buffer) >= self.part_bytes:
            self._upload(bytes(self.buffer[: self.part_bytes]))
            del self.buffer[: self.part_bytes]
        return len(data)

    def tell(self) -> int:
        return self.written

    def flush(self) -> None:
        """Parts are uploaded as they fill; nothing to do between them."""

    def _upload(self, body: bytes) -> None:
        number = len(self.parts) + 1
        response = self.s3.upload_part(
            Bucket=self.bucket, Key=self.key, UploadId=self.upload_id, PartNumber=number, Body=body
        )
        self.parts.append({"ETag": response["ETag"], "PartNumber": number})

    def complete(self) -> None:
        if self.buffer or not self.parts:
            self._upload(bytes(self.buffer))
            self.buffer = bytearray()
        self.s3.complete_multipart_upload(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            MultipartUpload={"Parts": self.parts},
        )

    def abort(self) -> None:
        try:
            self.s3.abort_multipart_upload(
                Bucket=self.bucket, Key=self.key, UploadId=self.upload_id
            )
        except Exception:  # noqa: BLE001 - the lifecycle rule removes an upload left behind
            LOGGER.warning("could not abort the multipart upload of %s", self.key)


def _zip_info(path: str, compress: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=dt.datetime.now(dt.timezone.utc).timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    return info


def stream_archive(plan: Plan, readme_text: str, writer: Any, s3: Any = None) -> None:
    """Writes the plan's files and README.md to ``writer`` as a ZIP, an object at a time, in chunks."""
    s3 = s3 or S3
    with zipfile.ZipFile(writer, mode="w", allowZip64=True) as archive:
        for entry in plan.files:
            compress = not entry["path"].lower().endswith(MEDIA_EXTENSIONS)
            info = _zip_info(entry["path"], compress)
            if "data" in entry:
                archive.writestr(info, entry["data"])
                continue
            body = s3.get_object(Bucket=RECORDINGS_BUCKET, Key=entry["key"])["Body"]
            try:
                with archive.open(info, mode="w", force_zip64=True) as target:
                    for chunk in body.iter_chunks(chunk_size=READ_CHUNK_BYTES):
                        target.write(chunk)
            finally:
                body.close()
        archive.writestr(_zip_info("README.md", True), readme_text.encode("utf-8"))


# --- entry points ------------------------------------------------------------------


def prepare(event: Mapping[str, Any], context: Any) -> Dict[str, Any]:
    arguments = event.get("arguments") or {}
    call_id = str(arguments.get("CallId") or "")
    contents = str(arguments.get("Contents") or "EVERYTHING")
    if not call_id or contents not in CONTENTS:
        raise BadRequestError("CallId and a known Contents are required")
    caller = caller_of(event)
    meeting = require_meeting(caller, call_id)
    status = {
        "CallId": call_id,
        "ArchiveId": uuid.uuid4().hex,
        "Contents": contents,
        "Status": PREPARING,
        "FileName": archive_file_name(call_id, contents),
        "RequestedBy": caller["username"] or ("backend" if caller["is_backend"] else ""),
        "IncludeConsent": contents == "EVERYTHING" and may_read_consent(caller, meeting),
        "CreatedAt": now_iso(),
        "UpdatedAt": now_iso(),
    }
    existing = reusable_archive(call_id, status, meeting)
    if existing is not None:
        return public_status(existing)
    write_status(status)
    LAMBDA.invoke(
        FunctionName=context.invoked_function_arn,
        InvocationType="Event",
        Payload=json.dumps(
            {"Trigger": "build", "CallId": call_id, "ArchiveId": status["ArchiveId"]}
        ),
    )
    return public_status(status)


def public_status(status: Mapping[str, Any]) -> Dict[str, Any]:
    answer = {
        k: status.get(k)
        for k in (
            "CallId",
            "ArchiveId",
            "Contents",
            "Status",
            "FileName",
            "Size",
            "Message",
            "CreatedAt",
            "UpdatedAt",
        )
    }
    answer["DownloadUrl"] = None
    if status.get("Status") == READY and status.get("ArchiveKey"):
        answer["DownloadUrl"] = S3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": RECORDINGS_BUCKET,
                "Key": status["ArchiveKey"],
                "ResponseContentType": "application/zip",
                "ResponseContentDisposition": f'attachment; filename="{status.get("FileName") or "meeting.zip"}"',
            },
            ExpiresIn=LINK_SECONDS,
        )
    return answer


def get_archive(event: Mapping[str, Any]) -> Dict[str, Any]:
    arguments = event.get("arguments") or {}
    call_id = str(arguments.get("CallId") or "")
    archive_id = str(arguments.get("ArchiveId") or "")
    if not call_id or not ARCHIVE_ID.match(archive_id):
        raise BadRequestError("CallId and ArchiveId are required")
    caller = caller_of(event)
    require_meeting(caller, call_id)
    status = read_status(call_id, archive_id)
    requester = caller["username"] or ("backend" if caller["is_backend"] else "")
    if status is None or not (caller["is_admin"] or status.get("RequestedBy") == requester):
        raise ForbiddenError("Unauthorized: no such archive")
    if status.get("Status") == PREPARING and _age_seconds(status.get("CreatedAt")) > STALE_SECONDS:
        status = {
            **status,
            "Status": FAILED,
            "Message": "The archive could not be finished in time.",
        }
    return public_status(status)


def _age_seconds(stamp: Any) -> float:
    try:
        created = dt.datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return 0.0
    return (dt.datetime.now(dt.timezone.utc) - created).total_seconds()


def build(call_id: str, archive_id: str) -> Dict[str, Any]:
    status = read_status(call_id, archive_id)
    if status is None or status.get("Status") != PREPARING:
        LOGGER.info("no archive %s of %s to build", archive_id, call_id)
        return {"ok": False}
    meeting = read_meeting(call_id)
    if meeting is None:
        write_status(
            {
                **status,
                "Status": FAILED,
                "Message": "The meeting no longer exists.",
                "UpdatedAt": now_iso(),
            }
        )
        return {"ok": False}
    writer = None
    try:
        plan = plan_archive(
            call_id, meeting, status["Contents"], bool(status.get("IncludeConsent"))
        )
        if plan.total > MAX_ARCHIVE_BYTES:
            raise ArchiveTooLargeError(
                f"The meeting's files come to {plan.total / 1024**3:.1f} GB, more than the "
                f"{MAX_ARCHIVE_BYTES / 1024**3:.0f} GB an archive can hold. Download the recordings on their own."
            )
        key = f"{archive_folder(call_id, archive_id)}{status['FileName']}"
        writer = MultipartWriter(S3, RECORDINGS_BUCKET, key)
        stream_archive(
            plan,
            readme(call_id, status["Contents"], plan, bool(status.get("IncludeConsent"))),
            writer,
        )
        writer.complete()
        done = {
            **status,
            "Status": READY,
            "ArchiveKey": key,
            "Size": writer.written,
            "UpdatedAt": now_iso(),
        }
        write_status(done)
        LOGGER.info(
            "archive %s of %s: %d files, %d bytes",
            archive_id,
            call_id,
            len(plan.files),
            writer.written,
        )
        return {"ok": True}
    except ArchiveTooLargeError as error:
        failed = {**status, "Status": FAILED, "Message": str(error), "UpdatedAt": now_iso()}
    except Exception:  # noqa: BLE001 - recorded for the caller, who is polling the status
        LOGGER.exception("could not build archive %s of %s", archive_id, call_id)
        failed = {
            **status,
            "Status": FAILED,
            "Message": "The archive could not be prepared.",
            "UpdatedAt": now_iso(),
        }
    if writer is not None:
        writer.abort()
    write_status(failed)
    return {"ok": False}


def handler(event: Mapping[str, Any], context: Any) -> Any:
    # Only this function invokes itself, and only without an identity -- an
    # AppSync caller always arrives with one.
    if event.get("Trigger") == "build" and event.get("identity") is None:
        return build(str(event.get("CallId") or ""), str(event.get("ArchiveId") or ""))
    field = ((event.get("info") or {}).get("fieldName")) or ""
    try:
        if field == "prepareMeetingArchive":
            return prepare(event, context)
        if field == "getMeetingArchive":
            return get_archive(event)
        raise BadRequestError(f"unknown field {field}")
    except (ForbiddenError, BadRequestError) as error:
        # AppSync shows a raised error's message to the caller.
        raise RuntimeError(str(error)) from None
