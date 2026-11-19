# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tool: get_meeting_record -- what LMA holds about one meeting, section by section.

Each section is read from where the meeting page reads it:

* details, summary -- the meeting's row (getCall).
* transcript, chat_links -- the stored transcript segments, speakers named as
  the meeting page names them; links found in the meeting chat by the
  configuration layer's rule (lma_chat_links), as the summary's chat section
  finds them.
* key_frames -- getVideoKeyframes; chat_attachments -- getChatAttachments;
  consent -- getMeetingConsent. Each resolver signs its links for an hour.
* recordings -- the audio and video recordings named on the meeting's row,
  signed here for the same hour.

Every link in the answer is a pre-signed S3 URL that stops working after
``LINK_SECONDS``; the answer says when. Unsigned storage locations (the S3 URLs
on the stored rows) are not returned.

The consent record names the meeting's participants and holds screenshots of
the meeting, so it is returned to the meeting's owner and Admins only, as the
rest of the record is returned to everyone getCall answers.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import boto3
from botocore.config import Config as BotoCoreConfig

from tools.access import Caller, can_manage, require_meeting
from tools.appsync_client import graphql
from tools.get_summary import keyframe_images_as_text
from tools.transcript_rows import as_line, kind_of, namer_for, read_final_rows
from tools.url_helper import get_meeting_url

LINK_SECONDS = 3600
SECTIONS = (
    "details",
    "summary",
    "transcript",
    "chat_links",
    "key_frames",
    "chat_attachments",
    "recordings",
    "consent",
)
DEFAULT_SECTIONS = ("details", "summary")
MAX_KEY_FRAMES = 200
MAX_ATTACHMENTS = 200
# getVideoKeyframes and getChatAttachments pages followed at most.
MAX_PAGES = 10

GET_VIDEO_KEYFRAMES = """
query GetVideoKeyframes($callId: ID!, $nextToken: String) {
  getVideoKeyframes(callId: $callId, limit: 100, nextToken: $nextToken) {
    VideoKeyframes {
      FrameId
      Timestamp
      VideoOffset
      Title
      SlideText
      ContentKind
      DuplicateOf
      ImageSignedUrl
      ThumbnailSignedUrl
    }
    nextToken
  }
}
"""

GET_CHAT_ATTACHMENTS = """
query GetChatAttachments($callId: ID!, $nextToken: String) {
  getChatAttachments(callId: $callId, nextToken: $nextToken) {
    ChatAttachments {
      AttachmentId
      Timestamp
      Kind
      FileName
      Status
      NotStoredReason
      ContentType
      Size
      Title
      Summary
      SummaryState
      DownloadUrl
    }
    nextToken
  }
}
"""

GET_MEETING_CONSENT = """
query GetMeetingConsent($CallId: ID!) {
  getMeetingConsent(CallId: $CallId) {
    CallId
    CaptureAllowed
    Consent {
      Mode
      Rule
      Status
      CaptureAllowed
      NoticeText
      NoticePostedAt
      Participants
      StartedAt
      StartedBy
      UpdatedAt
      Events {
        ParticipantName
        Decision
        Source
        Text
        OccurredAt
        RecordedBy
      }
      Attestation {
        AttestedBy
        Method
        Statement
        AttestedAt
      }
      Evidence {
        EvidenceId
        Kind
        CapturedAt
        AddedAt
        Note
        SignedUrl
        ImageDeletedAt
        Verification {
          Verdict
          Summary
          VerifiedAt
        }
      }
    }
  }
}
"""


def _sections(include: Any) -> List[str]:
    if include in (None, "", []):
        return list(DEFAULT_SECTIONS)
    if isinstance(include, str):
        include = [part.strip() for part in include.split(",")]
    if not isinstance(include, list) or not all(isinstance(part, str) for part in include):
        raise ValueError(f"include must be a list of: {', '.join(SECTIONS)}")
    wanted = []
    for part in include:
        name = part.strip().lower()
        if name not in SECTIONS:
            raise ValueError(f"unknown section {part!r}; include takes: {', '.join(SECTIONS)}")
        if name not in wanted:
            wanted.append(name)
    return wanted


def _details(meeting: Dict[str, Any], call_id: str) -> Dict[str, Any]:
    return {
        "meetingId": call_id,
        "meetingName": meeting.get("MeetingTopic") or call_id,
        "status": meeting.get("Status", "UNKNOWN"),
        "startTime": meeting.get("CreatedAt"),
        "updatedAt": meeting.get("UpdatedAt"),
        "durationSeconds": float(meeting.get("TotalConversationDurationMillis") or 0) / 1000,
        "owner": meeting.get("Owner"),
        "agenda": meeting.get("Agenda"),
        "meetingUrl": get_meeting_url(call_id),
    }


def _paged(query: str, field: str, list_field: str, call_id: str, cap: int) -> List[Dict]:
    items: List[Dict] = []
    token: Optional[str] = None
    for _ in range(MAX_PAGES):
        page = graphql(query, {"callId": call_id, "nextToken": token}).get(field) or {}
        items.extend(page.get(list_field) or [])
        token = page.get("nextToken")
        if not token or len(items) >= cap:
            break
    return items[:cap]


def s3_location(url: Any) -> Optional[tuple]:
    """The (bucket, key) of an S3 URL as LMA stores recording URLs, or None."""
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("s3://"):
        bucket, _, key = url[5:].partition("/")
        return (bucket, unquote(key)) if bucket and key else None
    parsed = urlparse(url)
    host = parsed.netloc
    if parsed.scheme != "https" or ".s3" not in host:
        return None
    path = unquote(parsed.path.lstrip("/"))
    if host.startswith("s3.") or host.startswith("s3-"):
        bucket, _, key = path.partition("/")
    else:
        bucket, key = host.split(".s3", 1)[0], path
    return (bucket, key) if bucket and key else None


def sign(url: Any) -> Optional[str]:
    """A recording's URL as a link valid for LINK_SECONDS; None when it is not in LMA's bucket."""
    location = s3_location(url)
    if not location or location[0] != os.environ.get("RECORDINGS_BUCKET"):
        return None
    client = boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION") or None,
        # SigV4 links on the regional endpoint, as the key-frame and attachment
        # resolvers sign theirs.
        config=BotoCoreConfig(
            signature_version="s3v4",
            s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"},
        ),
    )
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": location[0], "Key": location[1]},
        ExpiresIn=LINK_SECONDS,
    )


def execute(caller: Caller, meeting_id: str, include: Any = None) -> Dict[str, Any]:
    """The requested sections of the meeting's record."""
    sections = _sections(include)
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()
    if "consent" in sections and not can_manage(meeting, caller):
        raise PermissionError(
            "The consent record is returned to the meeting's owner and Admins only; "
            "ask again without 'consent'"
        )

    record: Dict[str, Any] = {"meetingId": call_id}
    if "details" in sections:
        record["details"] = _details(meeting, call_id)
    if "summary" in sections:
        record["summary"] = keyframe_images_as_text(meeting.get("CallSummaryText"))

    if "transcript" in sections or "chat_links" in sections:
        rows = [r for r in read_final_rows(call_id) if kind_of(r.get("Channel")) != "assistant"]
        rows.sort(key=lambda row: (float(row.get("StartTime") or 0), str(row.get("SegmentId"))))
        if "transcript" in sections:
            name = namer_for(rows, meeting)
            record["transcript"] = [as_line(row, name) for row in rows]
        if "chat_links" in sections:
            record["chatLinks"] = _chat_links(rows)

    if "key_frames" in sections:
        frames = _paged(
            GET_VIDEO_KEYFRAMES, "getVideoKeyframes", "VideoKeyframes", call_id, MAX_KEY_FRAMES
        )
        record["keyFrames"] = frames
    if "chat_attachments" in sections:
        record["chatAttachments"] = _paged(
            GET_CHAT_ATTACHMENTS, "getChatAttachments", "ChatAttachments", call_id, MAX_ATTACHMENTS
        )
    if "recordings" in sections:
        record["recordings"] = {
            "audioUrl": sign(meeting.get("RecordingUrl")),
            "videoUrl": sign(meeting.get("VideoRecordingUrl")),
        }
    if "consent" in sections:
        record["consent"] = graphql(GET_MEETING_CONSENT, {"CallId": call_id}).get(
            "getMeetingConsent"
        )

    if any(
        name in sections for name in ("key_frames", "chat_attachments", "recordings", "consent")
    ):
        expires = datetime.now(timezone.utc) + timedelta(seconds=LINK_SECONDS)
        record["linksExpireInSeconds"] = LINK_SECONDS
        record["linksExpireAt"] = expires.isoformat().replace("+00:00", "Z")
    return record


def _chat_links(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every address in the meeting chat, once, with who shared it first and when."""
    try:
        # pylint: disable-next=import-error,import-outside-toplevel
        from lma_chat_links import extract_links
    except ImportError:  # the layer is absent only outside Lambda
        return []
    found: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        if kind_of(row.get("Channel")) != "chat":
            continue
        for url in extract_links(row.get("Transcript")):
            if url in seen:
                continue
            seen.add(url)
            found.append(
                {
                    "url": url,
                    "sharedBy": row.get("Speaker"),
                    "time": float(row.get("StartTime") or 0),
                }
            )
    return found
