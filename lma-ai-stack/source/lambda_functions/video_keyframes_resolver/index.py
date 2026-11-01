#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for ``Query.getVideoKeyframes``: a meeting's key frames, with
short-lived links to their images.

The frames are items under the meeting (``kf#<CallId>``, written by the call
event processor); their images are private objects in the recordings bucket.
This answers the same callers ``getCall`` answers -- an Admin, the meeting's
owner, somebody it is shared with, or a backend role signing with IAM -- and for
them signs a GET for each image, so the people who can read the transcript can
see the pictures without the browser's identity being given read access to the
bucket. See docs/meeting-video.md.

A link is signed only for an object in this deployment's recordings bucket under
the meeting's own key-frame folder, the folder meeting deletion removes; a
stored URL pointing anywhere else is returned unsigned.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from typing import Any, Dict, Literal, Mapping, Optional
from urllib.parse import unquote, urlparse

import boto3
from botocore.config import Config as BotoCoreConfig

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)
EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
KEYFRAMES_PREFIX = os.environ.get("KEYFRAMES_PREFIX", "lma-video-keyframes/")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
LINK_SECONDS = int(os.environ.get("LINK_SECONDS", "3600"))

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
FRAME_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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


def _shared_with(raw: Any) -> set:
    if isinstance(raw, str):
        return {s.strip() for s in raw.strip("[]").split(",") if s.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(s).strip() for s in raw if s}
    return set()


def require_call_access(caller: Mapping[str, Any], call_id: str) -> None:
    """The ``getCall`` rule: Admin, owner, shared-with, or the backend.

    call_cost_resolver applies the same rule. This copy also reads a SharedWith
    stored as a list, which is how meeting sharing writes it, and as the
    "[a, b]" string a meeting created shared carries.
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
    shared = _shared_with(
        shared_raw.get("S") or shared_raw.get("SS") or [v.get("S") for v in shared_raw.get("L", [])]
    )
    username = caller["username"]
    if username and (owner == username or username in shared):
        return
    # The same answer for a meeting that does not exist as for one the caller
    # may not see, so the field cannot be used to test which meetings exist.
    LOGGER.info("refused key frames of %s to %s", call_id, caller["subject"] or "(none)")
    raise ForbiddenError("Unauthorized: you do not have access to this meeting")


def object_key(url: Any, call_id: str) -> Optional[str]:
    """The key of a stored image URL, when it is in this meeting's key-frame folder."""
    if not isinstance(url, str) or not RECORDINGS_BUCKET:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    # The whole bucket name, which may itself contain dots, ahead of the S3
    # host: https://<bucket>.s3.<region>.<suffix>/<key>, as the producers write.
    if not parsed.hostname.startswith(f"{RECORDINGS_BUCKET}.s3."):
        return None
    key = unquote(parsed.path.lstrip("/"))
    folder = f"{KEYFRAMES_PREFIX}{posixify_filename(call_id)}/"
    if not key.startswith(folder) or ".." in key:
        return None
    return key


def sign(key: Optional[str]) -> Optional[str]:
    if key is None:
        return None
    return S3.generate_presigned_url(
        "get_object",
        Params={"Bucket": RECORDINGS_BUCKET, "Key": key},
        ExpiresIn=LINK_SECONDS,
    )


def _value(attr: Mapping[str, Any]) -> Any:
    """One DynamoDB attribute value, for the scalar types a key frame item holds."""
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
    "FrameId",
    "Timestamp",
    "VideoOffset",
    "ImageUrl",
    "ThumbnailUrl",
    "Width",
    "Height",
    "Source",
    "ShareDetected",
    "ChangeScore",
    "Owner",
    "SharedWith",
)


def to_keyframe(item: Mapping[str, Any], call_id: str) -> Dict[str, Any]:
    """One item in the ``VideoKeyframe`` GraphQL shape, with signed image links."""
    frame = {name: _value(item[name]) for name in FIELDS if name in item}
    shared = frame.get("SharedWith")
    if isinstance(shared, list):
        # The type is a String, as on the call and the transcript segments.
        frame["SharedWith"] = ", ".join(str(s) for s in shared)
    frame["ImageSignedUrl"] = sign(object_key(frame.get("ImageUrl"), call_id))
    frame["ThumbnailSignedUrl"] = sign(object_key(frame.get("ThumbnailUrl"), call_id))
    return frame


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
    if not isinstance(key, dict) or key.get("PK") != {"S": f"kf#{call_id}"}:
        raise BadRequestError("nextToken is not valid for this meeting")
    sort_key = key.get("SK")
    if not isinstance(sort_key, dict) or not isinstance(sort_key.get("S"), str):
        raise BadRequestError("nextToken is not valid")
    return {"PK": key["PK"], "SK": {"S": sort_key["S"]}}


def get_video_keyframes(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """A page of the meeting's key frames in time order."""
    call_id = arguments.get("callId")
    if not isinstance(call_id, str) or not call_id:
        raise BadRequestError("callId is required")
    limit = arguments.get("limit")
    if limit is None:
        limit = DEFAULT_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise BadRequestError("limit must be a whole number")
    limit = max(1, min(MAX_LIMIT, limit))
    after = arguments.get("afterFrameId")
    if after is not None and (not isinstance(after, str) or not FRAME_ID.match(after)):
        raise BadRequestError("afterFrameId is not a frame id")

    query: Dict[str, Any] = {
        "TableName": EVENT_SOURCING_TABLE,
        "ScanIndexForward": True,
        "Limit": limit,
        "ExpressionAttributeValues": {":pk": {"S": f"kf#{call_id}"}},
    }
    if after:
        query["KeyConditionExpression"] = "PK = :pk AND SK > :after"
        query["ExpressionAttributeValues"][":after"] = {"S": f"f#{after}"}
    else:
        query["KeyConditionExpression"] = "PK = :pk"
    start = decode_token(arguments.get("nextToken"), call_id)
    if start:
        query["ExclusiveStartKey"] = start

    response = DYNAMODB.query(**query)
    frames = [to_keyframe(item, call_id) for item in response.get("Items", [])]
    return {"VideoKeyframes": frames, "nextToken": encode_token(response.get("LastEvaluatedKey"))}


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """AppSync Lambda resolver entry point (``callCostLambda.js``-style payload)."""
    field = ((event.get("info") or {}).get("fieldName")) or ""
    arguments = event.get("arguments") or {}
    if field != "getVideoKeyframes":
        raise BadRequestError(f"unsupported field {field}")
    call_id = arguments.get("callId")
    if not isinstance(call_id, str) or not call_id:
        raise BadRequestError("callId is required")
    require_call_access(caller_of(event), call_id)
    return get_video_keyframes(arguments)
