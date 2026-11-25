# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

#
import dataclasses
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from os import environ
from typing import Any
from urllib.parse import unquote, urlparse

import auto_share
import boto3
from appsync_utils import AppsyncRequestsGqlClient
from boto3.dynamodb.conditions import Attr
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import ClientError
from gql import gql  # noqa: F401
from gql.dsl import DSLMutation, DSLQuery, DSLSchema, dsl_gql

# Concurrency tuning.  DynamoDB easily absorbs dozens of in-flight deletes per
# meeting, but we cap both dials so a huge bulk delete can't saturate the
# connection pools or DynamoDB per-partition WCU on-demand limits.
MEETING_CONCURRENCY = int(environ.get("DELETE_MEETING_CONCURRENCY", "8"))
SEGMENT_CONCURRENCY = int(environ.get("DELETE_SEGMENT_CONCURRENCY", "16"))
# S3 DeleteObjects hard-caps at 1000 keys per request.
S3_BATCH_DELETE_LIMIT = 1000


APPSYNC_GRAPHQL_URL = environ["APPSYNC_GRAPHQL_URL"]
appsync_client = AppsyncRequestsGqlClient(url=APPSYNC_GRAPHQL_URL, fetch_schema_from_transport=True)

# grab environment variables
LCA_CALL_EVENTS_TABLE = environ["LCA_CALL_EVENTS_TABLE"]
S3_BUCKET_NAME = environ["S3_BUCKET_NAME"]
S3_RECORDINGS_PREFIX = environ["S3_RECORDINGS_PREFIX"]
S3_TRANSCRIPTS_PREFIX = environ["S3_TRANSCRIPTS_PREFIX"]
# Key frames of the shared screen (docs/meeting-video.md): images under this
# prefix, one folder per meeting, and items under PK kf#<CallId>.
S3_KEYFRAMES_PREFIX = environ.get("S3_KEYFRAMES_PREFIX", "lma-video-keyframes/")
S3_CONSENT_EVIDENCE_PREFIX = environ.get("S3_CONSENT_EVIDENCE_PREFIX", "lma-consent-evidence/")
# Archives of a meeting's files prepared for download (meeting_archive): one
# folder per meeting, removed with the meeting as well as by their lifecycle rule.
S3_MEETING_ARCHIVES_PREFIX = environ.get("S3_MEETING_ARCHIVES_PREFIX", "lma-meeting-archives/")
# Chat attachments (docs/virtual-participant.md): files under this prefix, one
# folder per meeting, and items under PK att#<CallId>.
S3_CHAT_ATTACHMENTS_PREFIX = environ.get("S3_CHAT_ATTACHMENTS_PREFIX", "lma-chat-attachments/")
# Meeting video recordings (docs/meeting-video.md): the final .mp4 under the
# first prefix, and the Virtual Participant's in-progress segments under
# <chunks prefix><CallId>/ until the .mp4 is assembled.
S3_VIDEO_RECORDINGS_PREFIX = environ.get("S3_VIDEO_RECORDINGS_PREFIX", "lma-video-recordings/")
S3_VIDEO_CHUNKS_PREFIX = environ.get("S3_VIDEO_CHUNKS_PREFIX", "lma-video-chunks/")
VP_TABLE_NAME = environ.get("VP_TABLE_NAME")
VP_TASK_REGISTRY_TABLE_NAME = environ.get("VP_TASK_REGISTRY_TABLE_NAME")
KINESIS_STREAM_NAME = environ.get("KINESIS_STREAM_NAME")

logger = logging.getLogger(__name__)
# Sized for MEETING_CONCURRENCY meetings each deleting SEGMENT_CONCURRENCY
# transcript items at once, plus the other steps of a meeting's deletion;
# botocore's default of 10 connections would serialise them.
ddb = boto3.resource(
    "dynamodb",
    config=BotoCoreConfig(max_pool_connections=MEETING_CONCURRENCY * SEGMENT_CONCURRENCY + 16),
)
ddbTable = ddb.Table(LCA_CALL_EVENTS_TABLE)
s3_client = boto3.client("s3")
kinesis_client = boto3.client("kinesis")
ecs_client = boto3.client("ecs")

# Common functions


def posixify_filename(filename: str) -> str:
    # Replace all invalid characters with underscores
    regex = r"[^a-zA-Z0-9_.]"
    posix_filename = re.sub(regex, "_", filename)
    # Remove leading and trailing underscores
    posix_filename = re.sub(r"^_+", "", posix_filename)
    posix_filename = re.sub(r"_+$", "", posix_filename)
    return posix_filename


def _delete_versions(bucket: str, entries: list[dict]) -> None:
    """Delete the listed object versions; raise if S3 reports any not deleted."""
    response = s3_client.delete_objects(Bucket=bucket, Delete={"Objects": entries, "Quiet": True})
    errors = response.get("Errors") or []
    if errors:
        raise RuntimeError(
            f"could not delete {len(errors)} object version(s) in {bucket}: "
            + "; ".join(f"{e.get('Key')} {e.get('Code')}" for e in errors[:3])
        )


def _version_entries(page: Mapping[str, Any], key: str | None = None) -> list[dict]:
    """The Key and VersionId of every version and delete marker on a listing page.

    With ``key``, only the entries of exactly that key: a listing by prefix also
    answers keys that merely begin with it.
    """
    return [
        {"Key": entry["Key"], "VersionId": entry["VersionId"]}
        for entry in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])
        if key is None or entry.get("Key") == key
    ]


def _batch_delete_prefix(bucket: str, prefix: str) -> int:
    """Delete every version of every object under ``prefix``; the number of keys.

    The recordings bucket is versioned, so deleting only the current version
    would leave the earlier ones behind as noncurrent versions. Every version
    and delete marker is listed and deleted in ``DeleteObjects`` batches of up
    to 1000, as transcript_deletion deletes a transcript's files. A version S3
    does not delete raises, so the meeting's deletion reports the failure.
    """
    keys: set[str] = set()
    paginator = s3_client.get_paginator("list_object_versions")
    batch: list[dict] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in _version_entries(page):
            keys.add(entry["Key"])
            batch.append(entry)
            if len(batch) >= S3_BATCH_DELETE_LIMIT:
                _delete_versions(bucket, batch)
                batch = []
    if batch:
        _delete_versions(bucket, batch)
    return len(keys)


def delete_recordings_transcripts(callid):
    filename = posixify_filename(f"{callid}")
    recordings_prefix = f"{S3_RECORDINGS_PREFIX}{filename}"
    transcripts_prefix = f"{S3_TRANSCRIPTS_PREFIX}{filename}"
    # The trailing slash keeps one meeting's folder from matching another whose
    # id merely begins the same way.
    archives_prefix = f"{S3_MEETING_ARCHIVES_PREFIX}{filename}/"

    # Run the prefix-scans in parallel: they hit different key spaces
    # (recordings, transcripts, download archives) so there's no contention.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_batch_delete_prefix, S3_BUCKET_NAME, recordings_prefix): "recordings",
            pool.submit(_batch_delete_prefix, S3_BUCKET_NAME, transcripts_prefix): "transcripts",
            pool.submit(_batch_delete_prefix, S3_BUCKET_NAME, archives_prefix): "download archives",
        }
        for fut in as_completed(futures):
            kind = futures[fut]
            try:
                n = fut.result()
                if n:
                    logger.info("Deleted %d %s objects for %s", n, kind, callid)
            except Exception as exc:  # noqa: BLE001
                logger.error("Error deleting %s for %s: %s", kind, callid, exc)
                raise


def _recording_key_from_url(url, prefix):
    """The recordings-bucket key a stored recording URL points at, or None.

    The producers store ``https://<bucket>.s3.<region>.<suffix>/<key>``, with the
    key URL-encoded by the Virtual Participant and as-is by the transcriber. Only
    a key in this deployment's bucket and directly under ``prefix`` is returned,
    so a deletion stays inside that kind of recording.
    """
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if not (parsed.hostname or "").startswith(f"{S3_BUCKET_NAME}.s3."):
        return None
    key = unquote(parsed.path.lstrip("/"))
    name = key[len(prefix) :]
    if not key.startswith(prefix) or not name or "/" in name:
        return None
    return key


def _video_key_from_url(url):
    """The key a stored VideoRecordingUrl points at, under the video prefix."""
    return _recording_key_from_url(url, S3_VIDEO_RECORDINGS_PREFIX)


def _delete_stored_recording(key, callid, kind):
    """Delete every version of one recording file.

    1 if the file had a version and S3 deleted them all, 0 if it had none left
    (a transcriber recording named by RecordingUrl has usually been removed
    already by the CallId-prefix scan, so 0 is common). A version S3 does not
    delete is logged and raises, as it does for the prefix scan, so the meeting
    record that names the file is kept and the deletion can be run again.
    """
    versions = []
    paginator = s3_client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=key):
        versions.extend(_version_entries(page, key))
    if not versions:
        return 0
    errors = []
    for start in range(0, len(versions), S3_BATCH_DELETE_LIMIT):
        response = s3_client.delete_objects(
            Bucket=S3_BUCKET_NAME,
            Delete={"Objects": versions[start : start + S3_BATCH_DELETE_LIMIT], "Quiet": True},
        )
        errors.extend(response.get("Errors") or [])
    for error in errors:
        logger.error(
            "Could not delete %s recording %s of %s: %s",
            kind,
            error.get("Key"),
            callid,
            error.get("Code"),
        )
    if errors:
        raise RuntimeError(
            f"could not delete {len(errors)} version(s) of the {kind} recording of {callid}"
        )
    return 1


def _stored_urls(current, earlier=None):
    """The recording links a meeting record names, each once.

    ``current`` is RecordingUrl or VideoRecordingUrl, ``earlier`` the matching
    RecordingUrls or VideoRecordingUrls list, which holds the link of every
    session of a meeting continued by a rejoin (and can name one twice).
    """
    urls = []
    for url in [*(earlier if isinstance(earlier, list) else []), current]:
        if isinstance(url, str) and url and url not in urls:
            urls.append(url)
    return urls


def delete_audio_recording(callid, audio_url=None, earlier_urls=None):
    """Delete the audio recording files the meeting record's links name.

    delete_recordings_transcripts removes the files whose names begin with the
    meeting's CallId, which is how the transcriber names its recordings. The
    Virtual Participant names its file from the meeting name and the time the
    recording was written, so that file is found only through the URL stored on
    the meeting: RecordingUrl, and RecordingUrls (``earlier_urls``) for every
    session of a meeting continued by a rejoin. Returns the number of files
    deleted; raises when S3 does not delete one of their versions (see
    _delete_stored_recording).
    """
    if not callid:
        return 0
    deleted = 0
    for url in _stored_urls(audio_url, earlier_urls):
        stored = _recording_key_from_url(url, S3_RECORDINGS_PREFIX)
        if not stored:
            continue
        if _delete_stored_recording(stored, callid, "audio"):
            deleted += 1
            logger.info("Deleted audio recording %s for %s", stored, callid)
    return deleted


def delete_video_recordings(callid, video_url=None, earlier_urls=None):
    """Delete the meeting's video recordings and any leftover recording segments.

    Only keys known exactly are deleted: the files the meeting record's
    VideoRecordingUrl and VideoRecordingUrls (``earlier_urls``, every session of
    a meeting continued by a rejoin) name, and the Virtual Participant's segments
    under ``<chunks prefix><CallId>/``, a folder named with the CallId as it is
    (the trailing slash keeps one meeting's folder from matching another whose
    id merely begins the same way). The video file names themselves are not
    searched: both producers turn the CallId into a file name by replacing
    characters, so two meetings can share one, and a name is no proof of whose
    file it is. Returns the number of recording files deleted; raises when S3
    does not delete one of their versions.
    """
    if not callid:
        return 0
    deleted = 0
    for url in _stored_urls(video_url, earlier_urls):
        stored = _video_key_from_url(url)
        if stored:
            deleted += _delete_stored_recording(stored, callid, "video")
    segments = _batch_delete_prefix(S3_BUCKET_NAME, f"{S3_VIDEO_CHUNKS_PREFIX}{callid}/")
    if deleted or segments:
        logger.info(
            "Deleted %d video recording(s) and %d segment(s) for %s", deleted, segments, callid
        )
    return deleted


def get_call_details(appsync_session, schema, callid):
    try:
        query = dsl_gql(
            DSLQuery(
                schema.Query.getCall.args(CallId=callid).select(
                    schema.Call.PK,
                    schema.Call.SK,
                    schema.Call.CallId,
                    schema.Call.CreatedAt,
                    schema.Call.Owner,
                    schema.Call.SharedWith,
                    schema.Call.RecordingUrl,
                    schema.Call.VideoRecordingUrl,
                    schema.Call.RecordingUrls,
                    schema.Call.VideoRecordingUrls,
                )
            )
        )

        result = appsync_session.execute(query)
    except ClientError as err:
        logger.error(
            "Error getting call details %s: %s",
            err.response["Error"]["Code"],
            err.response["Error"]["Message"],
        )
        raise
    else:
        return result


def get_call_metadata(callid):
    pk = "c#" + callid
    try:
        metadata = ddbTable.get_item(Key={"PK": pk, "SK": pk}, TableName=LCA_CALL_EVENTS_TABLE)
    except ClientError as err:
        logger.error(
            "Error getting metadata from LCA Call Events table %s: %s",
            err.response["Error"]["Code"],
            err.response["Error"]["Message"],
        )
        raise
    else:
        return metadata["Item"]


def verify_permissions(event):
    request_owner = event["identity"]["username"]
    isAdminUser = False
    groups = event["identity"].get("groups")
    if groups:
        isAdminUser = "Admin" in groups

    calls = event["arguments"]["input"]["Calls"]
    for call in calls:
        callid = call["CallId"]
        call["ListPK"]  # noqa: F841
        call["ListSK"]  # noqa: F841

        metadata = get_call_metadata(callid)
        if metadata.get("Owner", "") != request_owner and not isAdminUser:
            return False

    return True


# Share Meetings


# One recipient of a share: an email address. "[" and "]" only ever separate
# SharedWith entries (as "," does, which already separates the recipients), so
# a recipient containing either is not an address that can be stored.
RECIPIENT_PATTERN = re.compile(r"[^@\[\]]+@[^@\[\]]+\.[^@\[\]]+")


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


#: The error shareMeetings answers when a meeting's sharing list is no longer the one
#: the caller read (ExpectedSharedWith); the share dialog shows it and reads the list again.
SHARING_LIST_CHANGED = "SharingListChanged"
SHARING_LIST_CHANGED_MESSAGE = "The sharing list changed; reopen to see the current list"

#: How long the share dialog's request gives the meetings' items, of AppSync's
#: 30 seconds for the whole request; what is left is completed from the meetings
#: table's stream (complete_sharing).
DIALOG_FANOUT_SECONDS = float(environ.get("SHARE_DIALOG_FANOUT_SECONDS", "20"))


class SharingListChanged(Exception):
    """A meeting's sharing list is not the one the share dialog read."""


def _recipient_list(recipients: str) -> list:
    """The recipients of a share, trimmed, each once, in the order given."""
    out: list = []
    for email in (recipients or "").split(","):
        email = email.strip()
        if email and email not in out:
            out.append(email)
    return out


def _is_list_row_of(call_id: str, list_pk: Any, list_sk: Any) -> bool:
    """Whether ListPK/ListSK name a row of the meetings list for this meeting."""
    return (
        isinstance(list_pk, str)
        and isinstance(list_sk, str)
        and list_pk.startswith("cls#")
        and list_sk.startswith("ts#")
        and list_sk.endswith(f"#id#{call_id}")
    )


def _expected_entries(call: Mapping[str, Any]):
    """The sharing list the caller read for this meeting, or None when it sent none."""
    expected = call.get("ExpectedSharedWith")
    if expected is None:
        return None
    return sorted(set(shared_with_entries(list(expected))))


def _read_meeting(call_id: str):
    pk = f"c#{call_id}"
    return ddbTable.get_item(Key={"PK": pk, "SK": pk}, ConsistentRead=True).get("Item")


def share_meetings(calls, owner, recipients, context=None):
    """The share dialog's save: give each meeting the recipients as its sharing list.

    Each meeting's SharedWith, its list row's SharedWith and the SharingFanout
    marker are written in one transaction (record_share); then, within
    DIALOG_FANOUT_SECONDS, complete_sharing gives the meetings' transcripts, key
    frames and chat attachments the list and tells anyone unshared. What that
    time does not cover is completed from the meetings table's stream.

    A call that sends ExpectedSharedWith (the share dialog does) is saved only if
    the meeting's sharing list is still that one: otherwise nothing is written,
    for any of the meetings, and the answer carries ErrorType SharingListChanged.
    """
    if recipients and not all(
        RECIPIENT_PATTERN.fullmatch(email.strip()) for email in recipients.split(",")
    ):
        return {"Result": "Invalid email address provided"}
    for call in calls:
        if not _is_list_row_of(call["CallId"], call.get("ListPK"), call.get("ListSK")):
            return {"Result": "Invalid meeting list key provided"}
    new_recipients = _recipient_list(recipients)
    started = time.monotonic()
    budget = DIALOG_FANOUT_SECONDS
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        budget = min(budget, context.get_remaining_time_in_millis() / 1000 - 10)
    deadline = started + max(0.0, budget)

    try:
        # Every meeting is checked before any is written, so a conflict changes nothing.
        for call in calls:
            expected = _expected_entries(call)
            if expected is None:
                continue
            item = _read_meeting(call["CallId"]) or {}
            if sorted(set(shared_with_entries(item.get("SharedWith")))) != expected:
                raise SharingListChanged(call["CallId"])
        for call in calls:
            record_share(
                call["CallId"],
                {"PK": call["ListPK"], "SK": call["ListSK"]},
                new_recipients,
                _expected_entries(call),
            )
    except SharingListChanged as changed:
        logger.info("The sharing list of %s changed since it was read", changed)
        return {
            "Calls": [],
            "Result": SHARING_LIST_CHANGED_MESSAGE,
            "ErrorType": SHARING_LIST_CHANGED,
            "Owner": owner,
        }

    runner = f"dialog-{uuid.uuid4().hex}"
    for call in calls:
        if time.monotonic() >= deadline:
            break
        try:
            complete_sharing(call["CallId"], deadline, runner=runner)
        except Exception:  # noqa: BLE001 - the marker stays, and the stream completes it
            logger.exception("Sharing the items of %s is left to the stream", call["CallId"])

    return {
        "Calls": [call["CallId"] for call in calls],
        "Result": "Meetings shared successfully",
        "Owner": owner,
        "SharedWith": recipients,
    }


def record_share(call_id: str, list_key: Mapping[str, str], recipients: list, expected) -> None:
    """Write the share dialog's sharing list on the meeting and its list row, with the marker.

    On condition that the meeting's SharedWith, and its marker, are still as read;
    a marker changed meanwhile by a fan-out's progress is read again and the write
    retried, while a SharedWith that is no longer ``expected`` (when given) raises
    SharingListChanged.
    """
    for _attempt in range(3):
        item = _read_meeting(call_id)
        if not item:
            raise ValueError(f"meeting {call_id} not found")
        current = shared_with_entries(item.get("SharedWith"))
        if expected is not None and sorted(set(current)) != expected:
            raise SharingListChanged(call_id)
        removed = [entry for entry in current if entry not in recipients]
        fanout = _new_fanout(item, recipients, removed)
        writes = _sharing_write(
            call_id, item, None, list(recipients), list_key, fanout, list_row_must_exist=False
        )
        try:
            _transact(writes)
            return
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            logger.info("The record of %s changed while it was shared; reading it again", call_id)
    raise SharingListChanged(call_id)


def _keyframe_keys(callid):
    """The PK/SK of every key frame stored for the meeting."""
    kwargs = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": f"kf#{callid}"},
        "ProjectionExpression": "PK, SK",
    }
    while True:
        response = ddbTable.query(**kwargs)
        yield from response.get("Items", [])
        last = response.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def delete_keyframes(callid):
    """Delete the meeting's key frames: their items and their images.

    The images are found by the meeting's folder, which the producers name with
    the same posixify rule as posixify_filename. The trailing slash keeps one
    meeting's prefix from matching another whose id merely begins the same way.
    """
    deleted = 0
    for key in _keyframe_keys(callid):
        ddbTable.delete_item(Key={"PK": key["PK"], "SK": key["SK"]})
        deleted += 1
    images = _batch_delete_prefix(
        S3_BUCKET_NAME, f"{S3_KEYFRAMES_PREFIX}{posixify_filename(callid)}/"
    )
    if deleted or images:
        logger.info("Deleted %d key frame(s) and %d image(s) for %s", deleted, images, callid)
    return deleted


def _chat_attachment_keys(callid):
    """The PK/SK of every chat attachment stored for the meeting."""
    kwargs = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": f"att#{callid}"},
        "ProjectionExpression": "PK, SK",
    }
    while True:
        response = ddbTable.query(**kwargs)
        yield from response.get("Items", [])
        last = response.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def delete_chat_attachments(callid):
    """Delete the meeting's chat attachments: their items and every version of their files.

    The files are found by the meeting's folder, named with the same posixify
    rule as the key frames'; the trailing slash keeps one meeting's folder from
    matching another whose id merely begins the same way.
    """
    deleted = 0
    for key in _chat_attachment_keys(callid):
        ddbTable.delete_item(Key={"PK": key["PK"], "SK": key["SK"]})
        deleted += 1
    files = _batch_delete_prefix(
        S3_BUCKET_NAME, f"{S3_CHAT_ATTACHMENTS_PREFIX}{posixify_filename(callid)}/"
    )
    if deleted or files:
        logger.info("Deleted %d chat attachment(s) and %d file(s) for %s", deleted, files, callid)
    return deleted


def delete_consent_evidence(callid):
    """Delete the meeting's consent evidence images.

    The consent record itself is an attribute of the meeting row and goes with
    it. The images are found by the meeting's folder, named with the same
    posixify rule as the key frames' (see docs/meeting-consent.md).
    """
    images = _batch_delete_prefix(
        S3_BUCKET_NAME, f"{S3_CONSENT_EVIDENCE_PREFIX}{posixify_filename(callid)}/"
    )
    if images:
        logger.info("Deleted %d consent evidence image(s) for %s", images, callid)
    return images


# Delete Meetings


def _tune_requests_pool(appsync_session, size: int) -> None:
    """Expand the underlying urllib3 connection pool so concurrent GraphQL
    requests don't serialise on the default 10-connection cap.  Safe no-op
    if the transport internals change in a future gql version.
    """
    try:
        from requests.adapters import HTTPAdapter

        rs = appsync_session.transport.session  # type: ignore[attr-defined]
        adapter = HTTPAdapter(pool_connections=size, pool_maxsize=size, max_retries=0)
        rs.mount("https://", adapter)
        rs.mount("http://", adapter)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not tune requests pool: %s", exc)


def delete_meetings(calls, owner):
    """Delete multiple meetings in parallel.

    We open a single AppSync session (schema fetch is ~200 ms; amortised
    across the batch) and fan the per-meeting work across a ThreadPoolExecutor.
    gql's SyncClientSession.execute() ultimately calls requests.Session.send()
    which is thread-safe for concurrent callers, so sharing the session across
    workers is OK at our cap of ``MEETING_CONCURRENCY`` threads.

    Failures in any single meeting propagate (after waiting for in-flight
    futures) so the overall response still reports the failure rather than
    silently swallowing it.
    """
    if not calls:
        return {"Result": "No meetings to delete"}

    with appsync_client as appsync_session:
        if not appsync_session.client.schema:
            raise ValueError("invalid AppSync schema")
        schema = DSLSchema(appsync_session.client.schema)

        # Expand the underlying requests pool so the MEETING_CONCURRENCY
        # meetings' GraphQL calls don't serialise on the default 10-connection
        # limit. Each meeting makes them one at a time.
        _tune_requests_pool(appsync_session, MEETING_CONCURRENCY + 4)

        # Single meeting: skip the thread pool so we don't pay the pool
        # overhead for the common delete-one-meeting-from-detail-page path.
        if len(calls) == 1:
            call = calls[0]
            delete_meeting(
                appsync_session,
                schema,
                call["CallId"],
                call["ListPK"],
                call["ListSK"],
                owner,
            )
            return {"Result": "Meetings deleted successfully"}

        first_exc = None
        workers = max(1, min(MEETING_CONCURRENCY, len(calls)))
        logger.info("Deleting %d meetings with concurrency=%d", len(calls), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    delete_meeting,
                    appsync_session,
                    schema,
                    call["CallId"],
                    call["ListPK"],
                    call["ListSK"],
                    owner,
                ): call["CallId"]
                for call in calls
            }
            for fut in as_completed(futures):
                cid = futures[fut]
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.error("Error deleting meeting %s: %s", cid, exc)
                    if first_exc is None:
                        first_exc = exc
        if first_exc is not None:
            raise first_exc

    return {"Result": "Meetings deleted successfully"}


def delete_meeting(appsync_session, schema, callid, listPK, listSK, owner):
    try:
        # Run the independent preparation steps in parallel:
        #  1. Virtual Participant cleanup (paginated scan + ECS/DDB writes)
        #  2. Deletion of every item of the meeting's transcript (PK trs#<CallId>)
        #  3. Fetch call details (needed to preserve SharedWith on deleteCall)
        #  4. S3 prefix deletion
        #  5. Deletion of the meeting's metering records (PK mtr#<CallId>)
        #  6. Deletion of the meeting's key frames (PK kf#<CallId>, and images)
        #  7. Deletion of the meeting's consent evidence images
        #  8. Deletion of the meeting's chat attachments (PK att#<CallId>, and files)
        #  9. Deletion of its notifications and every user's view of them (PK nt#<CallId>)
        # 10. Deletion of its whiteboards, their versions and state (PK wb#<CallId>)
        with ThreadPoolExecutor(max_workers=10) as pool:
            vp_future = pool.submit(cleanup_virtual_participants, callid)
            segments_future = pool.submit(delete_transcript_items, callid)
            details_future = pool.submit(get_call_details, appsync_session, schema, callid)
            # S3 cleanup is independent of AppSync/DDB state, so start it now.
            s3_future = pool.submit(delete_recordings_transcripts, callid)
            metering_future = pool.submit(delete_metering_records, callid)
            keyframes_future = pool.submit(delete_keyframes, callid)
            evidence_future = pool.submit(delete_consent_evidence, callid)
            attachments_future = pool.submit(delete_chat_attachments, callid)
            notifications_future = pool.submit(delete_meeting_notifications, callid)
            whiteboards_future = pool.submit(delete_meeting_whiteboards, callid)

            # Surface any exception from the parallel preparation steps.
            vp_future.result()
            segments_future.result()
            details_result = details_future.result()
            s3_future.result()
            metering_future.result()
            keyframes_future.result()
            evidence_future.result()
            attachments_future.result()
            notifications_future.result()
            whiteboards_future.result()

        call_details = (details_result or {}).get("getCall") or {}
        shared_with = call_details.get("SharedWith")

        # After the details, because the stored links name the files: the
        # current RecordingUrl and VideoRecordingUrl, and the lists that hold
        # every session's.
        delete_audio_recording(
            callid, call_details.get("RecordingUrl"), call_details.get("RecordingUrls")
        )
        delete_video_recordings(
            callid, call_details.get("VideoRecordingUrl"), call_details.get("VideoRecordingUrls")
        )

        input = {
            "CallId": callid,
            "ListPK": listPK,
            "ListSK": listSK,
            "Owner": owner,
            "SharedWith": shared_with,
        }

        # Finally delete the call records (PK that begins with c# and cls#).
        # We do this *after* the parallel prep so an error above doesn't leave
        # the DDB call rows orphaned.
        mutation = dsl_gql(
            DSLMutation(
                schema.Mutation.deleteCall.args(input=input).select(
                    schema.DeleteCallOutput.CallId,
                    schema.DeleteCallOutput.Owner,
                    schema.DeleteCallOutput.SharedWith,
                )
            )
        )
        appsync_session.execute(mutation)
    except ClientError as err:
        logger.error(
            "Error deleting meetings %s: %s",
            err.response["Error"]["Code"],
            err.response["Error"]["Message"],
        )
        raise
    else:
        return


def delete_metering_records(callid):
    """Delete the meeting's metering records (PK ``mtr#<CallId>``).

    They hold the usage behind the meeting's cost estimate and would otherwise
    outlive a deleted meeting until their TTL. Item by item with DeleteItem,
    which this function is already granted: a meeting has tens of these, not
    thousands.
    """
    return _delete_partition(f"mtr#{callid}", "metering record", callid)


def delete_meeting_notifications(callid):
    """Delete the meeting's notifications (PK ``nt#<CallId>``), and the older guidance rows.

    The partition holds the notifications, their dedup pointers, the per-minute
    publish counters and each user's view of them (what they dismissed and
    muted); each expires with the meeting, so without this a deleted meeting's
    would remain until then. ``gd#<CallId>`` goes too: it holds the live guidance
    analyzer's state, and guidance a development build kept there.
    """
    deleted = _delete_partition(f"nt#{callid}", "notification row", callid)
    return deleted + _delete_partition(f"gd#{callid}", "guidance item", callid)


def delete_meeting_whiteboards(callid):
    """Delete the meeting's whiteboards (PK ``wb#<CallId>``): each whiteboard, its
    versions, the count, the write counters and the assistant's state. Each expires
    with the transcript, so without this a deleted meeting's would remain until then.

    It runs beside the other steps while the meeting row still exists, so a
    whiteboard write committed in that moment can remain after it. Such a row
    still expires with the transcript, and once deleteCall removes the meeting
    row the resolver refuses every further write, since each write checks it.
    """
    return _delete_partition(f"wb#{callid}", "whiteboard row", callid)


def _delete_partition(pk, kind, callid):
    """Delete every item under one partition key, item by item, across pages."""
    kwargs = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": pk},
        "ProjectionExpression": "PK, SK",
    }
    deleted = 0
    while True:
        response = ddbTable.query(**kwargs)
        for item in response.get("Items", []):
            ddbTable.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            deleted += 1
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    if deleted:
        logger.info("Deleted %d %s(s) for %s", deleted, kind, callid)
    return deleted


def _transcript_item_keys(callid):
    """The keys of every item under the meeting's transcript partition."""
    kwargs = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": f"trs#{callid}"},
        "ProjectionExpression": "PK, SK",
        "ConsistentRead": True,
    }
    while True:
        response = ddbTable.query(**kwargs)
        for item in response.get("Items", []):
            yield {"PK": item["PK"], "SK": item["SK"]}
        last = response.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def delete_transcript_items(callid):
    """Delete every item of the meeting's transcript (PK ``trs#<CallId>``).

    That is every segment whatever its channel, partial or final, read straight
    from the table rather than through getTranscriptSegments, which answers only
    the segments it can return. transcript_deletion empties the same partition
    when it deletes a transcript. Item by item with DeleteItem, which this
    function is already granted, SEGMENT_CONCURRENCY at a time.
    """
    keys = list(_transcript_item_keys(callid))
    if not keys:
        return 0

    workers = max(1, min(SEGMENT_CONCURRENCY, len(keys)))
    first_exc = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(ddbTable.delete_item, Key=key) for key in keys]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.error("Error deleting a transcript item for %s: %s", callid, exc)
                if first_exc is None:
                    first_exc = exc
    if first_exc is not None:
        raise first_exc
    logger.info("Deleted %d transcript item(s) for %s", len(keys), callid)
    return len(keys)


def _virtual_participants_of(vp_table, callid):
    """Every Virtual Participant record whose CallId is the meeting's.

    The table is keyed by the Virtual Participant's id alone, with no index on
    CallId, so it is scanned, page after page until DynamoDB returns no
    LastEvaluatedKey: a filtered page can hold none of the matches, or only some.
    """
    kwargs = {"FilterExpression": Attr("CallId").eq(callid)}
    while True:
        response = vp_table.scan(**kwargs)
        yield from response.get("Items", [])
        last = response.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def cleanup_virtual_participants(callid):
    """End and delete every Virtual Participant record of the meeting being deleted.

    For each record, a Virtual Participant still in the meeting is sent an END
    event and its task is stopped (both best effort: one that has already left
    has nothing to stop), then the record and its task registry entry are
    deleted. Reading the table or deleting a record that fails raises, after
    every record has been tried, so the meeting's deletion reports it as it
    reports any other step's failure, and the meeting record is kept for the
    deletion to be run again. Returns the number of records deleted.
    """
    if not VP_TABLE_NAME:
        logger.info("VP_TABLE_NAME not configured, skipping VP cleanup")
        return 0

    vp_table = ddb.Table(VP_TABLE_NAME)
    deleted = 0
    failures = []
    for vp in _virtual_participants_of(vp_table, callid):
        vp_id = vp.get("id")
        if not vp_id:
            continue

        logger.info("Cleaning up VP %s for meeting %s", vp_id, callid)
        send_vp_end_event(callid, vp)
        stop_vp_ecs_task(vp_id)
        try:
            vp_table.delete_item(Key={"id": vp_id})
        except Exception as exc:  # noqa: BLE001
            logger.error("Error deleting VP %s of meeting %s: %s", vp_id, callid, exc)
            failures.append(vp_id)
            continue
        delete_vp_registry_entry(vp_id)
        deleted += 1

    if deleted:
        logger.info("Cleaned up %d VPs for meeting %s", deleted, callid)
    if failures:
        raise RuntimeError(
            f"could not delete {len(failures)} Virtual Participant record(s) of meeting {callid}"
        )
    return deleted


def send_vp_end_event(callid, vp):
    """Send END event to Kinesis for VP cleanup"""
    if not KINESIS_STREAM_NAME:
        return

    try:
        end_event = {
            "EventType": "END",
            "CallId": callid,
            "CustomerPhoneNumber": "Virtual Participant",
            "SystemPhoneNumber": "LMA System",
            "CreatedAt": datetime.now(timezone.utc).isoformat(),
            "AgentId": vp.get("owner", "Unknown"),
            "AccessToken": "",
            "IdToken": "",
            "RefreshToken": "",
        }

        kinesis_client.put_record(
            StreamName=KINESIS_STREAM_NAME,
            PartitionKey=callid,
            Data=json.dumps(end_event).encode("utf-8"),
        )

        logger.info(f"Sent END event to Kinesis for VP in meeting {callid}")

    except Exception as e:
        logger.error(f"Error sending VP END event: {e}")


def stop_vp_ecs_task(vp_id):
    """Stop ECS task for VP using registry lookup"""
    if not VP_TASK_REGISTRY_TABLE_NAME:
        return

    try:
        registry_table = ddb.Table(VP_TASK_REGISTRY_TABLE_NAME)
        response = registry_table.get_item(Key={"vpId": vp_id})

        task_details = response.get("Item")
        if not task_details:
            logger.info(f"No task registry entry found for VP {vp_id}")
            return

        task_arn = task_details.get("taskArn")
        cluster_arn = task_details.get("clusterArn")

        if task_arn and cluster_arn:
            ecs_client.stop_task(
                cluster=cluster_arn, task=task_arn, reason=f"Meeting deleted - VP {vp_id} cleanup"
            )
            logger.info(f"Stopped ECS task for VP {vp_id}")

    except Exception as e:
        logger.error(f"Error stopping ECS task for VP {vp_id}: {e}")


def delete_vp_registry_entry(vp_id):
    """Delete VP registry entry"""
    if not VP_TASK_REGISTRY_TABLE_NAME:
        return

    try:
        registry_table = ddb.Table(VP_TASK_REGISTRY_TABLE_NAME)
        registry_table.delete_item(Key={"vpId": vp_id})
        logger.info(f"Deleted registry entry for VP {vp_id}")

    except Exception as e:
        logger.error(f"Error deleting registry entry for VP {vp_id}: {e}")


# Sharing with the participant list (Meeting Sharing; see auto_share.py)


_CONFIGURATION_STORE = None


def _auto_share_enabled() -> bool:
    """Meeting Sharing › Share meetings with their listed participants; on unless saved off.

    Read now (the section's tier is immediate), and never assumed: if the
    section cannot be read the error is raised, so the stream record is retried
    rather than shared under a setting an Admin may have turned off. A section
    that does not hold the setting has the shipped default, on.
    """
    global _CONFIGURATION_STORE  # noqa: PLW0603 -- one store per process
    from lma_config import (
        ConfigurationStore,
        ConfigurationTableNotConfigured,
    )  # configuration layer

    try:
        if _CONFIGURATION_STORE is None:
            _CONFIGURATION_STORE = ConfigurationStore()
        values = _CONFIGURATION_STORE.get_section("meeting_sharing").values or {}
    except ConfigurationTableNotConfigured:
        # A function deployed without the table name; every deployed copy has it.
        logger.warning("CONFIGURATION_TABLE_NAME is unset; Meeting Sharing is not applied")
        return False
    return values.get("shareWithListedParticipants", True) is not False


# The user pool, to share with an account under its own username.
USER_POOL_ID = environ.get("VITE_USER_POOL_ID", "")
_cognito = None


def account_username(address: str) -> str:
    """The username of the account with this address, as getCall compares it; else the address.

    The user pool compares usernames ignoring case, so an address listed as
    Jane.Doe@Example.com finds the account made as jane.doe@example.com, and the
    share is written under that account's own username, which is what every
    reader of SharedWith compares with the caller's.
    """
    global _cognito  # noqa: PLW0603 -- one client per process
    if not USER_POOL_ID:
        return address
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    try:
        return (
            _cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=address).get("Username")
            or address
        )
    except _cognito.exceptions.UserNotFoundException:
        return address


def _stream_image(record: Mapping[str, Any]) -> dict:
    from boto3.dynamodb.types import TypeDeserializer

    image = (record.get("dynamodb") or {}).get("NewImage") or {}
    deserializer = TypeDeserializer()
    return {name: deserializer.deserialize(value) for name, value in image.items()}


def _auto_share_plan(item: Mapping[str, Any]) -> auto_share.AutoSharePlan:
    return auto_share.plan_auto_share(
        item.get("Owner"),
        shared_with_entries(item.get("SharedWith")),
        item.get("Participants"),
        item.get(auto_share.AUTO_SHARED_ATTRIBUTE),
    )


class SharingChanged(Exception):
    """The meeting's sharing changed between the read a plan was made from and its write."""


# ── Giving a meeting's items its sharing list ───────────────────────────────
#
# Sharing a meeting is two steps. The first writes, in one transaction, the
# meeting's SharedWith (with AutoSharedWith, for Meeting Sharing), its list
# row's SharedWith, and SharingFanout, a marker saying that the meeting's items
# have still to be given that list:
#
#   {"Token": <new for every share>,
#    "Unshared": [<addresses to tell they no longer have access>],
#    "Cursor": {"Stage": "trs" | "kf" | "att", "After": <SK of the last item done>}}
#
# The second, complete_sharing, gives every item of the meeting's transcript
# (PK trs#), key frames (kf#) and chat attachments (att#) the meeting's
# SharedWith as it is then, tells the Unshared addresses, and removes the
# marker. While the marker is there the share is unfinished, whatever else the
# record says, and every write of the meeting record reaches the meetings
# table's stream, whose handler runs complete_sharing for any record that has
# the marker. So a fan-out that fails, or a function that times out, is taken
# up again: the next attempt starts from the Cursor, which a fan-out writes
# when its time is nearly up (that write is itself a record in the stream),
# and writing an item's SharedWith again is harmless.
#
# One fan-out at a time works on a meeting: SharingFanoutLease ({"By", "Until"},
# Until in epoch seconds) names it. The stream's handler is the runner "stream":
# the stream gives one invocation at a time each meeting (one batch per shard,
# a meeting's records on one shard), so it may always take over a lease of its
# own, left by an invocation that ended. The share dialog's request runs a
# fan-out of its own for its time (DIALOG_FANOUT_SECONDS), under a lease that
# ends soon after, and the stream waits for that lease to be released or to end.
#
# A share made while a fan-out is running writes a new Token; the fan-out sees
# it after the chunk it is writing and starts again from the first item with
# the new list, so the last list written to every item is the meeting's.

#: The meeting record's marker of a share whose items are not all given its list.
FANOUT_ATTRIBUTE = "SharingFanout"
#: The meeting record's attribute naming the fan-out working on it.
LEASE_ATTRIBUTE = "SharingFanoutLease"
#: The meeting's item partitions, in the order they are given the list:
#: transcript items, key frames, chat attachments.
FANOUT_STAGES = ("trs", "kf", "att")
#: Items given the list between two checks of the time and of the Token.
FANOUT_CHUNK = int(environ.get("SHARING_FANOUT_CHUNK", "100"))
#: The stream handler's runner name (see above).
STREAM_RUNNER = "stream"
#: A lease lasts this much past its holder's deadline.
LEASE_MARGIN_SECONDS = 30
#: How often a runner waiting for another's lease reads the record again.
LEASE_POLL_SECONDS = 2.0
#: The stream handler stops starting work this long before its function times out.
STREAM_MARGIN_SECONDS = 30

_FANOUT_NAMES = {"#fan": FANOUT_ATTRIBUTE, "#tok": "Token"}
_LEASE_NAMES = {"#lease": LEASE_ATTRIBUTE, "#by": "By", "#until": "Until"}


def _key(address: str) -> str:
    return address.strip().casefold()


def _new_fanout(item: Mapping[str, Any], recipients, removed) -> dict:
    """The marker a share writes: a new Token, and whom to tell they were unshared.

    Those an unfinished earlier share was still to tell are kept, unless this
    share gives them the meeting again.
    """
    keep = {_key(a) for a in recipients or []}
    earlier = (item.get(FANOUT_ATTRIBUTE) or {}).get("Unshared") or []
    unshared: list = []
    seen: set = set()
    for address in list(earlier) + list(removed or []):
        if not isinstance(address, str) or not address.strip():
            continue
        key = _key(address)
        if key in keep or key in seen:
            continue
        seen.add(key)
        unshared.append(address.strip())
    fanout: dict = {"Token": uuid.uuid4().hex}
    if unshared:
        fanout["Unshared"] = unshared
    return fanout


def _sharing_write(
    call_id: str,
    item: Mapping[str, Any],
    plan,
    recipients,
    list_key,
    fanout=None,
    list_row_must_exist=True,
) -> list:
    """The transaction that records a share.

    SharedWith (``recipients``; None leaves it, [] removes it) and, with a
    ``plan``, AutoSharedWith on the meeting; SharedWith on its list row; and the
    ``fanout`` marker. Only if the meeting's SharedWith, and its marker, are still
    what was read.
    """
    pk = f"c#{call_id}"
    names = {"#sw": "SharedWith"}
    values: dict = {}
    sets, removes = [], []
    if recipients is not None:
        if recipients:
            sets.append("#sw = :sw")
            values[":sw"] = recipients
        else:
            removes.append("#sw")
    if plan is not None:
        names["#auto"] = auto_share.AUTO_SHARED_ATTRIBUTE
        if plan.auto_shared:
            sets.append("#auto = :auto")
            values[":auto"] = plan.auto_shared
        else:
            removes.append("#auto")
    if fanout is not None:
        names["#fan"] = FANOUT_ATTRIBUTE
        sets.append("#fan = :fan")
        values[":fan"] = fanout
    expression = " ".join(
        part
        for part in (
            f"SET {', '.join(sets)}" if sets else "",
            f"REMOVE {', '.join(removes)}" if removes else "",
        )
        if part
    )
    # The meeting must still exist, and hold the SharedWith the share was made from:
    # a share made meanwhile (the share dialog, or Meeting Sharing) is not overwritten.
    condition = "attribute_exists(PK) AND "
    if item.get("SharedWith") is None:
        condition += "attribute_not_exists(#sw)"
    else:
        condition += "#sw = :seen"
        values[":seen"] = item["SharedWith"]
    if fanout is not None:
        # Nor a marker another share wrote meanwhile, whose Unshared this one carries on.
        earlier = (item.get(FANOUT_ATTRIBUTE) or {}).get("Token")
        if earlier:
            names["#tok"] = "Token"
            condition += " AND #fan.#tok = :seentok"
            values[":seentok"] = earlier
        else:
            condition += " AND attribute_not_exists(#fan)"
    meeting = {
        "Update": {
            "TableName": LCA_CALL_EVENTS_TABLE,
            "Key": {"PK": pk, "SK": pk},
            "UpdateExpression": expression,
            "ConditionExpression": condition,
            "ExpressionAttributeNames": names,
            **({"ExpressionAttributeValues": values} if values else {}),
        }
    }
    writes = [meeting]
    if recipients is not None and list_key is not None:
        list_update = {
            "TableName": LCA_CALL_EVENTS_TABLE,
            "Key": {"PK": list_key["PK"], "SK": list_key["SK"]},
            "ExpressionAttributeNames": {"#sw": "SharedWith"},
        }
        if list_row_must_exist:
            list_update["ConditionExpression"] = "attribute_exists(PK)"
        if recipients:
            list_update["UpdateExpression"] = "SET #sw = :sw"
            list_update["ExpressionAttributeValues"] = {":sw": recipients}
        else:
            list_update["UpdateExpression"] = "REMOVE #sw"
        writes.append({"Update": list_update})
    return writes


def _transact(writes: list) -> None:
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()

    def typed(update: dict) -> dict:
        out = dict(update)
        out["Key"] = {k: serializer.serialize(v) for k, v in update["Key"].items()}
        if "ExpressionAttributeValues" in update:
            out["ExpressionAttributeValues"] = {
                k: serializer.serialize(v) for k, v in update["ExpressionAttributeValues"].items()
            }
        return out

    ddb.meta.client.transact_write_items(
        TransactItems=[{"Update": typed(write["Update"])} for write in writes]
    )


def apply_auto_share(call_id: str, item: Mapping[str, Any], plan: auto_share.AutoSharePlan) -> bool:
    """Record a Meeting Sharing plan; True when it left the meeting's items to be given the list.

    The meeting's SharedWith, AutoSharedWith and marker, and its list row's
    SharedWith, are written in one transaction, on condition that the meeting's
    SharedWith and marker are still the ones the plan was made from; if either
    changed meanwhile (the share dialog), SharingChanged is raised and the record
    is retried, so the plan is made again from that change.
    """
    if plan.add:
        # Each new address under its account's own username, where it has one.
        added = [account_username(address) for address in plan.add]
        kept = plan.auto_shared[: len(plan.auto_shared) - len(plan.add)]
        plan = dataclasses.replace(plan, add=added, auto_shared=kept + added)
    recipients = None
    list_key = None
    fanout = None
    if plan.changes_sharing:
        list_key = auto_share.list_row_key(call_id, item.get("CreatedAt"))
        if list_key is None:
            raise ValueError(f"meeting {call_id} has no CreatedAt to find its list row by")
        recipients = auto_share.shared_with_after(shared_with_entries(item.get("SharedWith")), plan)
        fanout = _new_fanout(item, recipients, plan.remove)
    try:
        _transact(_sharing_write(call_id, item, plan, recipients, list_key, fanout))
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = [r.get("Code") for r in err.response.get("CancellationReasons") or []]
        if not _read_meeting(call_id):
            logger.info("Meeting %s was deleted before its shares were recorded", call_id)
            return False
        raise SharingChanged(f"the sharing of {call_id} changed while it was planned ({reasons})")
    return fanout is not None


def _tell_unshared(appsync_session, schema, call_id: str, removed: list) -> None:
    """unshareCall, which tells the people a meeting is no longer shared with."""
    mutation = dsl_gql(
        DSLMutation(
            schema.Mutation.unshareCall.args(
                input={"CallId": call_id, "SharedWith": removed}
            ).select(schema.UnshareCallOutput.CallId, schema.UnshareCallOutput.SharedWith)
        )
    )
    appsync_session.execute(mutation)


def _notify_unshared(call_id: str, removed: list) -> None:
    with appsync_client as appsync_session:
        if not appsync_session.client.schema:
            raise ValueError("invalid AppSync schema")
        schema = DSLSchema(appsync_session.client.schema)
        _tell_unshared(appsync_session, schema, call_id, removed)


def _partition_keys(pk: str, after=None):
    """The keys of the items under ``pk`` in SK order, from the one after SK ``after``."""
    kwargs: dict = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": pk},
        "ProjectionExpression": "PK, SK",
        "ConsistentRead": True,
    }
    if after:
        kwargs["ExclusiveStartKey"] = {"PK": pk, "SK": after}
    while True:
        response = ddbTable.query(**kwargs)
        for item in response.get("Items", []):
            yield {"PK": item["PK"], "SK": item["SK"]}
        last = response.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def _chunks(keys, size: int):
    chunk: list = []
    for key in keys:
        chunk.append(key)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _give_item(key: Mapping[str, str], recipients: list) -> None:
    """One item given the sharing list; an item deleted meanwhile is left deleted."""
    kwargs: dict = {"Key": dict(key), "ConditionExpression": "attribute_exists(PK)"}
    if recipients:
        kwargs["UpdateExpression"] = "SET SharedWith = :val"
        kwargs["ExpressionAttributeValues"] = {":val": list(recipients)}
    else:
        kwargs["UpdateExpression"] = "REMOVE SharedWith"
    try:
        ddbTable.update_item(**kwargs)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def _give_items(pool, keys: list, recipients: list) -> None:
    for future in [pool.submit(_give_item, key, recipients) for key in keys]:
        future.result()


def _conditional_update(**kwargs) -> bool:
    """An UpdateItem on the meeting record; False when its condition did not hold."""
    try:
        ddbTable.update_item(**kwargs)
        return True
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _meeting_key(call_id: str) -> dict:
    return {"PK": f"c#{call_id}", "SK": f"c#{call_id}"}


def _claim_lease(call_id: str, runner: str, deadline: float) -> bool:
    until = int(time.time() + max(0.0, deadline - time.monotonic()) + LEASE_MARGIN_SECONDS)
    return _conditional_update(
        Key=_meeting_key(call_id),
        UpdateExpression="SET #lease = :lease",
        ConditionExpression=(
            "attribute_exists(#fan) AND (attribute_not_exists(#lease) OR #lease.#by = :me "
            "OR #lease.#until <= :now)"
        ),
        ExpressionAttributeNames={**_LEASE_NAMES, "#fan": FANOUT_ATTRIBUTE},
        ExpressionAttributeValues={
            ":lease": {"By": runner, "Until": until},
            ":me": runner,
            ":now": int(time.time()),
        },
    )


def _release_lease(call_id: str, runner: str) -> None:
    _conditional_update(
        Key=_meeting_key(call_id),
        UpdateExpression="REMOVE #lease",
        ConditionExpression="#lease.#by = :me",
        ExpressionAttributeNames={"#lease": LEASE_ATTRIBUTE, "#by": "By"},
        ExpressionAttributeValues={":me": runner},
    )


def _checkpoint(call_id: str, runner: str, token: str, position: Mapping[str, str]) -> None:
    """Record how far the fan-out got and release the lease; a record in the stream either way."""
    saved = _conditional_update(
        Key=_meeting_key(call_id),
        UpdateExpression="SET #fan.#cur = :cur REMOVE #lease",
        ConditionExpression="#fan.#tok = :tok AND #lease.#by = :me",
        ExpressionAttributeNames={
            **_FANOUT_NAMES,
            "#cur": "Cursor",
            "#lease": LEASE_ATTRIBUTE,
            "#by": "By",
        },
        ExpressionAttributeValues={":cur": dict(position), ":tok": token, ":me": runner},
    )
    if not saved:
        # A newer share's marker, which starts from the first item.
        _release_lease(call_id, runner)


def _finish(call_id: str, token: str) -> bool:
    """Remove the marker and the lease, if no share was made since the fan-out read it."""
    return _conditional_update(
        Key=_meeting_key(call_id),
        UpdateExpression="REMOVE #fan, #lease",
        ConditionExpression="#fan.#tok = :tok",
        ExpressionAttributeNames={**_FANOUT_NAMES, "#lease": LEASE_ATTRIBUTE},
        ExpressionAttributeValues={":tok": token},
    )


def _current_token(call_id: str):
    item = (
        ddbTable.get_item(
            Key=_meeting_key(call_id),
            ConsistentRead=True,
            ProjectionExpression="#fan.#tok",
            ExpressionAttributeNames=_FANOUT_NAMES,
        ).get("Item")
        or {}
    )
    return (item.get(FANOUT_ATTRIBUTE) or {}).get("Token")


def _lease_until(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def complete_sharing(call_id: str, deadline: float, runner: str = STREAM_RUNNER, wait=False) -> str:
    """Give the meeting's items its sharing list, if a share left them to be given it.

    ``deadline`` is a time.monotonic() value. Answers "done" (nothing left to do),
    "paused" (the time ran out, and the progress, or the lease's release, was
    written, which the stream takes up), or "busy" (another fan-out holds the
    lease; with ``wait`` that is answered only once the deadline comes).
    """
    while True:
        item = _read_meeting(call_id)
        if not item or not item.get(FANOUT_ATTRIBUTE):
            return "done"
        lease = item.get(LEASE_ATTRIBUTE) or {}
        holder = lease.get("By")
        held = holder and holder != runner and _lease_until(lease.get("Until")) > time.time()
        if not held and _claim_lease(call_id, runner, deadline):
            break
        # Another fan-out holds the meeting, or took it between the read and the claim.
        if not wait or time.monotonic() + LEASE_POLL_SECONDS >= deadline:
            return "busy"
        time.sleep(LEASE_POLL_SECONDS)
    try:
        return _fan_out(call_id, runner, deadline)
    except Exception:
        try:
            _release_lease(call_id, runner)
        except Exception:  # noqa: BLE001 - the lease ends by itself
            logger.warning("Could not release the sharing lease of %s", call_id)
        raise


def _fan_out(call_id: str, runner: str, deadline: float) -> str:
    workers = max(1, SEGMENT_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            item = _read_meeting(call_id) or {}
            marker = item.get(FANOUT_ATTRIBUTE)
            if not marker:
                _release_lease(call_id, runner)
                return "done"
            token = marker.get("Token")
            target = shared_with_entries(item.get("SharedWith"))
            cursor = marker.get("Cursor") or {}
            stage = cursor.get("Stage")
            start = FANOUT_STAGES.index(stage) if stage in FANOUT_STAGES else 0
            after = cursor.get("After") if stage in FANOUT_STAGES else None
            superseded = False
            for name in FANOUT_STAGES[start:]:
                for chunk in _chunks(_partition_keys(f"{name}#{call_id}", after), FANOUT_CHUNK):
                    _give_items(pool, chunk, target)
                    if time.monotonic() >= deadline:
                        _checkpoint(
                            call_id, runner, token, {"Stage": name, "After": chunk[-1]["SK"]}
                        )
                        return "paused"
                    if _current_token(call_id) != token:
                        superseded = True
                        break
                if superseded:
                    break
                after = None
            if superseded:
                continue
            keep = {_key(a) for a in target}
            unshared = [a for a in marker.get("Unshared") or [] if _key(a) not in keep]
            if unshared:
                _notify_unshared(call_id, unshared)
            if _finish(call_id, token):
                return "done"


def _stream_deadline(context) -> float:
    remaining = 300.0
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        remaining = context.get_remaining_time_in_millis() / 1000
    return time.monotonic() + max(0.0, remaining - STREAM_MARGIN_SECONDS)


def handle_meeting_stream(event: Mapping[str, Any], context=None) -> dict:
    """The meetings table's stream: Meeting Sharing, and the items of unfinished shares.

    The event source mapping passes only writes of items whose PK starts c#;
    of those, only meeting records (PK equal to SK) are read, and each meeting
    once per batch, from its latest record. The record in the stream decides
    whether anything is to be done, without a read: a participant list to share
    with, or a SharingFanout marker. When something is, the meeting is read
    again, consistently, and planned from what it holds now, since later writes
    of the same meeting can follow in the stream; then any share left unfinished
    is completed (complete_sharing). A meeting that fails, or that another
    fan-out still holds when time runs out, is reported, so the batch is retried
    from it.
    """
    latest: dict = {}
    for record in event.get("Records") or []:
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            continue
        keys = (record.get("dynamodb") or {}).get("Keys") or {}
        pk = (keys.get("PK") or {}).get("S", "")
        sk = (keys.get("SK") or {}).get("S", "")
        if not pk.startswith("c#") or pk != sk:
            continue
        latest[pk[2:]] = record
    deadline = _stream_deadline(context)
    failures = []
    enabled = None
    for call_id, record in latest.items():
        try:
            image = _stream_image(record)
            pending = bool(image.get(FANOUT_ATTRIBUTE))
            planned = _auto_share_plan(image).changes_anything
            if planned:
                if enabled is None:
                    enabled = _auto_share_enabled()
                planned = enabled
            if not pending and not planned:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("no time left in this invocation")
            if planned:
                item = _read_meeting(call_id)
                if not item:
                    continue
                plan = _auto_share_plan(item)
                if plan.changes_anything:
                    pending = apply_auto_share(call_id, item, plan) or pending
                    # Counts only: the addresses are personal data.
                    logger.info(
                        "Meeting sharing for %s: %d added, %d removed",
                        call_id,
                        len(plan.add),
                        len(plan.remove),
                    )
                pending = pending or bool(item.get(FANOUT_ATTRIBUTE))
            if not pending:
                continue
            outcome = complete_sharing(call_id, deadline, wait=True)
            if outcome == "busy":
                raise TimeoutError("another fan-out still holds the meeting")
            if outcome == "paused":
                logger.info("Sharing the items of %s continues from its next record", call_id)
        except Exception:  # noqa: BLE001 - reported for retry, the batch goes on
            logger.exception("Meeting sharing failed for %s", call_id)
            failures.append(
                {"itemIdentifier": (record.get("dynamodb") or {}).get("SequenceNumber", "")}
            )
    return {"batchItemFailures": failures}


def lambda_handler(event, context):
    # The meetings table's stream (Meeting Sharing), rather than an AppSync request.
    if "Records" in event:
        return handle_meeting_stream(event, context)
    owner = event["identity"]["username"]
    if not verify_permissions(event):
        return {
            "Result": "You don't have permission to share or delete one or more of the requested "
            "calls"
        }

    calls = event["arguments"]["input"]["Calls"]
    action = event["info"]["fieldName"]

    if action == "shareMeetings":
        recipients = event["arguments"]["input"]["MeetingRecipients"]
        return share_meetings(calls, owner, recipients, context)
    elif action == "deleteMeetings":
        return delete_meetings(calls, owner)
    else:
        return {"Result": "Invalid action"}
