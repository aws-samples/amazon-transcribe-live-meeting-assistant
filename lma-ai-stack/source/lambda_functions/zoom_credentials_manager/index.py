#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Zoom Credentials Manager Lambda.

AppSync direct-Lambda data source for the per-user Zoom credentials feature
and the per-user persisted Chrome profile.

Each LMA user can store one set of Zoom username/password in Secrets Manager
keyed by their Cognito sub. The plaintext password is never returned to the
client; getMyZoomCredentialsStatus returns only {present, username,
lastUpdatedAt}.

The user's persisted Chromium profile (cookies, saved-device markers) is
managed independently of the credentials so a user can wipe one without
the other.

Operations:
- setMyZoomCredentials(input: {username, password}) -> ZoomCredentialsStatus
- getMyZoomCredentialsStatus -> ZoomCredentialsStatus
- deleteMyZoomCredentials -> Boolean (also deletes the saved profile)
- getMyChromeProfileStatus -> ChromeProfileStatus
- deleteMyChromeProfile -> Boolean (credentials kept)

Secret name layout: ${LMA_STACK_NAME}/zoom-credentials/{cognito_sub}
Profile S3 key:     profiles/{sha256(cognito_sub.lower())}/profile.tar.gz
                    (must match lma-virtual-participant-stack profile-store.ts)
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets = boto3.client("secretsmanager")
s3 = boto3.client("s3")

LMA_STACK_NAME = os.environ.get("LMA_STACK_NAME", "lma")
KMS_KEY_ID = os.environ.get("CUSTOMER_MANAGED_KMS_KEY_ID", "")
VP_PROFILES_BUCKET = os.environ.get("VP_PROFILES_BUCKET", "")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._%+\-@]{3,254}$")
SUB_RE = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


def _secret_name(cognito_sub: str) -> str:
    return f"{LMA_STACK_NAME}/zoom-credentials/{cognito_sub}"


def _get_cognito_sub(event: Dict[str, Any]) -> Optional[str]:
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    sub = claims.get("sub") or identity.get("sub")
    if not sub or not SUB_RE.match(sub):
        logger.error("Missing or invalid Cognito sub on AppSync identity")
        return None
    return sub


def _validate_credentials(input_data: Dict[str, Any]) -> Optional[str]:
    username = input_data.get("username") or ""
    password = input_data.get("password") or ""
    if not username or len(username) > 254:
        return "username is required and must be 254 chars or fewer"
    if not USERNAME_RE.match(username):
        return "username must be a valid email or Zoom username"
    if not password or not (8 <= len(password) <= 256):
        return "password must be between 8 and 256 chars"
    return None


def _put_secret(secret_name: str, payload: Dict[str, Any]) -> None:
    """Create the secret if it doesn't exist, otherwise put_secret_value."""
    body = json.dumps(payload, sort_keys=True)
    try:
        secrets.put_secret_value(SecretId=secret_name, SecretString=body)
        return
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            pass  # fall through to create_secret
        elif code == "InvalidRequestException":
            # Likely a legacy secret left in scheduled-deletion state by an
            # earlier delete_my_zoom_credentials call. Restore and retry.
            described = secrets.describe_secret(SecretId=secret_name)
            if not described.get("DeletedDate"):
                raise
            secrets.restore_secret(SecretId=secret_name)
            secrets.put_secret_value(SecretId=secret_name, SecretString=body)
            return
        else:
            raise

    create_kwargs: Dict[str, Any] = {
        "Name": secret_name,
        "SecretString": body,
        "Description": f"LMA Zoom credentials for user (managed by {LMA_STACK_NAME})",
    }
    if KMS_KEY_ID:
        create_kwargs["KmsKeyId"] = KMS_KEY_ID
    secrets.create_secret(**create_kwargs)


def set_my_zoom_credentials(event: Dict[str, Any]) -> Dict[str, Any]:
    sub = _get_cognito_sub(event)
    if not sub:
        return {"present": False, "username": None, "lastUpdatedAt": None}

    input_data = event.get("arguments", {}).get("input", {}) or {}
    err = _validate_credentials(input_data)
    if err:
        logger.error("setMyZoomCredentials validation failed: %s", err)
        raise ValueError(err)

    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "username": input_data["username"],
        "password": input_data["password"],
        "updatedAt": now_iso,
    }
    secret_name = _secret_name(sub)
    _put_secret(secret_name, payload)
    logger.info("Stored Zoom credentials for user sub=%s as secret=%s", sub, secret_name)
    return {
        "present": True,
        "username": input_data["username"],
        "lastUpdatedAt": now_iso,
    }


def get_my_zoom_credentials_status(event: Dict[str, Any]) -> Dict[str, Any]:
    sub = _get_cognito_sub(event)
    if not sub:
        return {"present": False, "username": None, "lastUpdatedAt": None}
    secret_name = _secret_name(sub)
    try:
        described = secrets.describe_secret(SecretId=secret_name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "InvalidRequestException"):
            return {"present": False, "username": None, "lastUpdatedAt": None}
        raise

    if described.get("DeletedDate"):
        return {"present": False, "username": None, "lastUpdatedAt": None}

    try:
        value = secrets.get_secret_value(SecretId=secret_name)
        body = json.loads(value.get("SecretString") or "{}")
        username = body.get("username")
        last_updated_at = body.get("updatedAt")
    except (ClientError, json.JSONDecodeError) as exc:
        logger.warning("Could not read secret %s: %s", secret_name, exc)
        username = None
        last_updated_at = None
    if not last_updated_at:
        ts = described.get("LastChangedDate") or described.get("CreatedDate")
        if ts:
            last_updated_at = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    return {
        "present": True,
        "username": username,
        "lastUpdatedAt": last_updated_at,
    }


def _normalize_platform(platform: Optional[str]) -> str:
    # Mirror normalizePlatform() in
    # lma-virtual-participant-stack/backend/src/profile-store.ts so the S3 key
    # segment matches between the writer (VP backend) and this reader/deleter.
    p = (platform or "").strip().lower()
    if p.startswith("zoom"):
        return "zoom"
    if p.startswith("chime"):
        return "chime"
    if p.startswith("team"):
        return "teams"
    if p.startswith("webex"):
        return "webex"
    if p.startswith("google"):
        return "googlemeet"
    cleaned = re.sub(r"[^a-z0-9]+", "", p)
    return cleaned or "unknown"


def _user_profile_prefix(sub: str, platform: Optional[str] = None) -> str:
    # Must match lma-virtual-participant-stack/backend/src/profile-store.ts
    # which writes to profiles/{sha256(sub.lower())}/{platform}/profile.tar.gz.
    # The earlier version of this Lambda used the raw sub, leaving every saved
    # profile orphaned in S3 when the user clicked Remove.
    #
    # platform=None targets profiles/{userHash}/ (all of a user's platforms);
    # a specific platform narrows to that one platform's profile.
    user_hash = hashlib.sha256(sub.lower().encode("utf-8")).hexdigest()
    if platform:
        return f"profiles/{user_hash}/{_normalize_platform(platform)}/"
    return f"profiles/{user_hash}/"


def _delete_user_profiles(sub: str, platform: Optional[str] = None) -> int:
    """Delete the user's persisted Chromium profile prefix in S3.

    platform=None wipes every platform's profile for the user (used when Zoom
    credentials are removed). A specific platform wipes only that one.

    Returns the number of objects deleted. Best-effort — non-fatal on error.
    """
    if not VP_PROFILES_BUCKET:
        return 0
    prefix = _user_profile_prefix(sub, platform)
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=VP_PROFILES_BUCKET, Prefix=prefix):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if not objects:
                continue
            s3.delete_objects(Bucket=VP_PROFILES_BUCKET, Delete={"Objects": objects})
            deleted += len(objects)
    except ClientError as exc:
        logger.warning("Failed to wipe profile prefix %s: %s", prefix, exc)
    return deleted


def _describe_user_profile(sub: str, platform: Optional[str] = None) -> Dict[str, Any]:
    """Return {present, sizeBytes, lastModified} for the user's profile tar.

    Scoped to a single platform when given, else aggregated across all of the
    user's platform profiles.
    """
    out: Dict[str, Any] = {"present": False, "sizeBytes": None, "lastModified": None}
    if not VP_PROFILES_BUCKET:
        return out
    prefix = _user_profile_prefix(sub, platform)
    try:
        resp = s3.list_objects_v2(Bucket=VP_PROFILES_BUCKET, Prefix=prefix, MaxKeys=10)
    except ClientError as exc:
        logger.warning("Failed to head profile prefix %s: %s", prefix, exc)
        return out
    contents = resp.get("Contents") or []
    if not contents:
        return out
    out["present"] = True
    # The store layout is one tar per user; sum sizes anyway in case future
    # versions split the profile across multiple keys.
    out["sizeBytes"] = sum(int(o.get("Size") or 0) for o in contents)
    latest = max(contents, key=lambda o: o.get("LastModified") or datetime.min)
    lm = latest.get("LastModified")
    if lm:
        out["lastModified"] = lm.isoformat() if hasattr(lm, "isoformat") else str(lm)
    return out


def get_my_chrome_profile_status(event: Dict[str, Any]) -> Dict[str, Any]:
    sub = _get_cognito_sub(event)
    if not sub:
        return {"present": False, "sizeBytes": None, "lastModified": None}
    # Optional platform scopes the status to one platform's profile; omitted
    # aggregates across all of the user's platforms (back-compat).
    platform = (event.get("arguments", {}) or {}).get("platform")
    return _describe_user_profile(sub, platform)


def delete_my_chrome_profile(event: Dict[str, Any]) -> bool:
    sub = _get_cognito_sub(event)
    if not sub:
        return False
    platform = (event.get("arguments", {}) or {}).get("platform")
    deleted = _delete_user_profiles(sub, platform)
    logger.info(
        "Wiped %d Chromium profile object(s) for user sub=%s platform=%s",
        deleted,
        sub,
        platform or "ALL",
    )
    return True


def delete_my_zoom_credentials(event: Dict[str, Any]) -> bool:
    sub = _get_cognito_sub(event)
    if not sub:
        return False
    secret_name = _secret_name(sub)
    try:
        # Force-delete: "Remove" in the UI means gone, and a 7-day recovery
        # window blocks the user from re-adding credentials under the same
        # secret name (PutSecretValue / CreateSecret both fail with
        # InvalidRequestException while the secret is scheduled for deletion).
        secrets.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ResourceNotFoundException":
            logger.error("Failed to delete secret %s: %s", secret_name, exc)
            raise
    # Wipe the persisted ZOOM Chromium profile (cookies, "trusted device"
    # markers). Without this the next Zoom login session for the same user-sub
    # would outlive the credentials. Other platforms' profiles are unrelated to
    # Zoom credentials and are left intact.
    deleted = _delete_user_profiles(sub, "zoom")
    if deleted:
        logger.info("Wiped %d Zoom profile object(s) for user sub=%s", deleted, sub)
    return True


def handler(event: Dict[str, Any], _context: Any) -> Any:
    field_name = (event.get("info") or {}).get("fieldName", "")
    logger.info("ZoomCredentialsManager - Field: %s", field_name)
    if field_name == "setMyZoomCredentials":
        return set_my_zoom_credentials(event)
    if field_name == "getMyZoomCredentialsStatus":
        return get_my_zoom_credentials_status(event)
    if field_name == "deleteMyZoomCredentials":
        return delete_my_zoom_credentials(event)
    if field_name == "getMyChromeProfileStatus":
        return get_my_chrome_profile_status(event)
    if field_name == "deleteMyChromeProfile":
        return delete_my_chrome_profile(event)
    logger.error("Unknown field: %s", field_name)
    raise ValueError(f"Unknown operation: {field_name}")
