#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for ``Query.getChatAttachments``: a meeting's chat attachments,
with short-lived links to their content.

The attachments are items under the meeting (``att#<CallId>``, written by the
call event processor); their content is private objects in the recordings
bucket, under the meeting's own folder. This answers the same callers
``getCall`` answers -- an Admin, the meeting's owner, somebody it is shared
with, or a backend role signing with IAM -- and for them signs a GET for each
stored attachment, so the people who can read the transcript can open the
files without the browser's identity being given read access to the bucket.
See docs/virtual-participant.md, "Chat attachments".

Two links are signed for a stored attachment:

* ``DownloadUrl``, for every type: S3 is told to answer with
  ``Content-Disposition: attachment`` and the stored type, so the browser saves
  the file rather than showing it;
* ``PreviewUrl``, for a PNG, JPEG, GIF or WebP image only: ``inline`` with that
  image type, for the thumbnail on the chat line. No other type is ever served
  inline, and SVG and HTML are not stored at all.

A link is signed only for an object in this deployment's recordings bucket under
the meeting's own attachments folder, the folder meeting and transcript deletion
remove; a stored key pointing anywhere else is not signed.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from typing import Any, Dict, Literal, Mapping, Optional
from urllib.parse import quote

import boto3
from botocore.config import Config as BotoCoreConfig

# Stdlib logging rather than aws_lambda_powertools: this function carries no
# layer and uses the runtime's own boto3.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)
EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
ATTACHMENTS_PREFIX = os.environ.get("ATTACHMENTS_PREFIX", "lma-chat-attachments/")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
LINK_SECONDS = int(os.environ.get("LINK_SECONDS", "3600"))

PAGE_LIMIT = 200
ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: The types a thumbnail may be shown from, inline. Raster images only.
INLINE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
#: The types a download is served as, as stored; anything else is served as
#: application/octet-stream. The same list as the call event processor accepts.
DOWNLOAD_TYPES = INLINE_IMAGE_TYPES | frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "text/plain",
        "text/csv",
        "text/markdown",
    }
)


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


# Virtual-hosted links, except for a bucket whose name has dots: that name does
# not match S3's TLS certificate as a host name, so its links are path-style.
S3 = _s3_client("path" if "." in RECORDINGS_BUCKET else "virtual")


class ForbiddenError(Exception):
    """The caller may not see the meeting."""


class BadRequestError(Exception):
    """An argument that cannot be used."""


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

    The rule ``getCall``'s resolver applies, as call_cost_resolver reads it: a
    request signed with IAM by one of LMA's own roles carries an assumed-role
    ``userArn`` and no user-pool claims, and is the backend.
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


def require_call_access(caller: Mapping[str, Any], call_id: str) -> None:
    """The ``getCall`` rule: Admin, owner, shared-with, or the backend.

    The same check as video_keyframes_resolver's (each function is packaged on
    its own); keep the two the same.
    """
    if caller["is_admin"] or caller["is_backend"]:
        return
    response = DYNAMODB.get_item(
        TableName=EVENT_SOURCING_TABLE,
        Key={"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}},
        ProjectionExpression="#o, SharedWith",
        ExpressionAttributeNames={"#o": "Owner"},
    )
    item = response.get("Item") or {}
    owner = (item.get("Owner") or {}).get("S", "")
    shared_raw = item.get("SharedWith") or {}
    shared = shared_with_entries(
        shared_raw.get("S") or shared_raw.get("SS") or [v.get("S") for v in shared_raw.get("L", [])]
    )
    username = caller["username"]
    if username and (owner == username or username in shared):
        return
    # The same answer for a meeting that does not exist as for one the caller
    # may not see, so the field cannot be used to test which meetings exist.
    LOGGER.info("refused chat attachments of %s to %s", call_id, caller["subject"] or "(none)")
    raise ForbiddenError("Unauthorized: you do not have access to this meeting")


def object_key(key: Any, call_id: str) -> Optional[str]:
    """The stored key, when it is in this meeting's attachments folder."""
    if not RECORDINGS_BUCKET:
        return None
    folder = f"{ATTACHMENTS_PREFIX}{posixify_filename(call_id)}/"
    return key if key_in_folder(key, folder) else None


def content_disposition(disposition: str, file_name: Any) -> str:
    """A Content-Disposition value naming the file, safe for any name.

    The plain ``filename`` keeps only printable ASCII other than quotes and
    backslashes; ``filename*`` carries the name as typed, percent-encoded.
    """
    name = str(file_name or "attachment")
    plain = re.sub(r'[^\x20-\x7e]|["\\]', "_", name).strip() or "attachment"
    return f"{disposition}; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


def _sign(key: str, content_type: str, disposition: str) -> str:
    return S3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": RECORDINGS_BUCKET,
            "Key": key,
            "ResponseContentType": content_type,
            "ResponseContentDisposition": disposition,
        },
        ExpiresIn=LINK_SECONDS,
    )


def links(item: Mapping[str, Any], call_id: str) -> Dict[str, Optional[str]]:
    """The attachment's DownloadUrl and PreviewUrl, or None for each it does not get."""
    key = object_key(item.get("S3Key"), call_id)
    if item.get("Status") != "STORED" or key is None:
        return {"DownloadUrl": None, "PreviewUrl": None}
    stored_type = item.get("ContentType")
    download_type = stored_type if stored_type in DOWNLOAD_TYPES else "application/octet-stream"
    download = _sign(key, download_type, content_disposition("attachment", item.get("FileName")))
    preview = None
    if stored_type in INLINE_IMAGE_TYPES:
        preview = _sign(key, stored_type, "inline")
    return {"DownloadUrl": download, "PreviewUrl": preview}


def _value(attr: Mapping[str, Any]) -> Any:
    """One DynamoDB attribute value, for the scalar types an attachment item holds."""
    if "S" in attr:
        return attr["S"]
    if "N" in attr:
        number = float(attr["N"])
        return int(number) if number.is_integer() and "." not in attr["N"] else number
    if "BOOL" in attr:
        return attr["BOOL"]
    if "NULL" in attr:
        return None
    if "SS" in attr:
        return list(attr["SS"])
    if "L" in attr:
        return [_value(v) for v in attr["L"]]
    return None


FIELDS = (
    "PK",
    "SK",
    "CreatedAt",
    "UpdatedAt",
    "ExpiresAfter",
    "CallId",
    "AttachmentId",
    "SegmentId",
    "Timestamp",
    "Kind",
    "FileName",
    "Platform",
    "Status",
    "NotStoredReason",
    "ContentType",
    "Size",
    "S3Key",
    "Summarize",
    "Title",
    "Summary",
    "SummaryState",
    "Owner",
    "SharedWith",
)


def to_attachment(item: Mapping[str, Any], call_id: str) -> Dict[str, Any]:
    """One item in the ``ChatAttachment`` GraphQL shape, with signed links."""
    attachment = {name: _value(item[name]) for name in FIELDS if name in item}
    shared = attachment.get("SharedWith")
    if isinstance(shared, list):
        # The type is a String, as on the call and the transcript segments.
        attachment["SharedWith"] = ", ".join(str(s) for s in shared)
    attachment.update(links(attachment, call_id))
    return attachment


def encode_token(last_key: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not last_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def decode_token(token: Optional[str], call_id: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise BadRequestError("nextToken is not valid") from error
    if not isinstance(key, dict) or key.get("PK") != {"S": f"att#{call_id}"}:
        raise BadRequestError("nextToken is not valid for this meeting")
    sort_key = key.get("SK")
    if not isinstance(sort_key, dict) or not isinstance(sort_key.get("S"), str):
        raise BadRequestError("nextToken is not valid")
    return {"PK": key["PK"], "SK": {"S": sort_key["S"]}}


def get_chat_attachments(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """A page of the meeting's attachments in time order, or the one named."""
    call_id = arguments["callId"]
    attachment_id = arguments.get("attachmentId")
    if attachment_id is not None:
        if not isinstance(attachment_id, str) or not ATTACHMENT_ID.match(attachment_id):
            raise BadRequestError("attachmentId is not an attachment id")
        found = DYNAMODB.get_item(
            TableName=EVENT_SOURCING_TABLE,
            Key={"PK": {"S": f"att#{call_id}"}, "SK": {"S": f"a#{attachment_id}"}},
        ).get("Item")
        return {
            "ChatAttachments": [to_attachment(found, call_id)] if found else [],
            "nextToken": None,
        }
    query: Dict[str, Any] = {
        "TableName": EVENT_SOURCING_TABLE,
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": f"att#{call_id}"}},
        "ScanIndexForward": True,
        "Limit": PAGE_LIMIT,
    }
    start = decode_token(arguments.get("nextToken"), call_id)
    if start:
        query["ExclusiveStartKey"] = start
    response = DYNAMODB.query(**query)
    return {
        "ChatAttachments": [to_attachment(item, call_id) for item in response.get("Items", [])],
        "nextToken": encode_token(response.get("LastEvaluatedKey")),
    }


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """AppSync Lambda resolver entry point (``videoKeyframesLambda.js``-style payload)."""
    field = ((event.get("info") or {}).get("fieldName")) or ""
    arguments = event.get("arguments") or {}
    if field != "getChatAttachments":
        raise BadRequestError(f"unsupported field {field}")
    call_id = arguments.get("callId")
    if not isinstance(call_id, str) or not call_id:
        raise BadRequestError("callId is required")
    require_call_access(caller_of(event), call_id)
    return get_chat_attachments(arguments)
