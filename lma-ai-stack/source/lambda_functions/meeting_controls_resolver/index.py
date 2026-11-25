# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

#
import json
import logging
import re
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


def get_transcript_segments(appsync_session, schema, callid):
    """Every final segment of the meeting, read a page at a time.

    A long meeting's segments span several pages of the table, and a page can
    come back empty with a nextToken, so pages are read until there is none.
    """
    segments = []
    next_token = None
    while True:
        query = dsl_gql(
            DSLQuery(
                schema.Query.getTranscriptSegments.args(callId=callid, nextToken=next_token).select(
                    schema.TranscriptSegmentList.TranscriptSegments.select(
                        schema.TranscriptSegment.PK,
                        schema.TranscriptSegment.SK,
                    ),
                    schema.TranscriptSegmentList.nextToken,
                )
            )
        )
        try:
            result = appsync_session.execute(query)
        except ClientError as err:
            logger.error(
                "Error reading transcript segments %s: %s",
                err.response["Error"]["Code"],
                err.response["Error"]["Message"],
            )
            raise
        page = result.get("getTranscriptSegments") or {}
        segments.extend(segment for segment in page.get("TranscriptSegments") or [] if segment)
        next_token = page.get("nextToken")
        if not next_token:
            return segments


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


def share_meetings(calls, owner, recipients):
    if recipients and not all(
        RECIPIENT_PATTERN.fullmatch(email.strip()) for email in recipients.split(",")
    ):
        return {"Result": "Invalid email address provided"}

    with appsync_client as appsync_session:
        if not appsync_session.client.schema:
            raise ValueError("invalid AppSync schema")
        schema = DSLSchema(appsync_session.client.schema)

        for call in calls:
            callid = call["CallId"]
            listPK = call["ListPK"]
            listSK = call["ListSK"]
            share_meeting(appsync_session, schema, callid, listPK, listSK, recipients, owner)

    callIds = [call["CallId"] for call in calls]
    return {
        "Calls": callIds,
        "Result": "Meetings shared successfully",
        "Owner": owner,
        "SharedWith": recipients,
    }


def share_meeting(appsync_session, schema, callid, listPK, listSK, recipients, owner):
    new_recipients = list(set(email.strip() for email in recipients.split(",") if email.strip()))

    if not recipients:
        new_recipients = None

    try:
        for transcript_segment in get_transcript_segments(appsync_session, schema, callid):
            update_transcript_segment(
                appsync_session,
                schema,
                transcript_segment["PK"],
                transcript_segment["SK"],
                new_recipients,
            )
        share_keyframes(callid, new_recipients)
        share_chat_attachments(callid, new_recipients)

        input = {
            "CallId": callid,
            "ListPK": listPK,
            "ListSK": listSK,
            "Owner": owner,
            "SharedWith": new_recipients,
        }

        result = get_call_details(appsync_session, schema, callid)
        shared_with = result.get("getCall").get("SharedWith")

        shared_with = shared_with_entries(shared_with)

        if shared_with and new_recipients:
            unshare_list = list(set(shared_with) - set(new_recipients))
        elif shared_with:
            unshare_list = shared_with
        else:
            unshare_list = []
        # Now share the call records (PK that begins with c# and cls#)
        mutation = dsl_gql(
            DSLMutation(
                schema.Mutation.shareCall.args(input=input).select(
                    schema.ShareCallOutput.CallId,
                    schema.ShareCallOutput.Owner,
                    schema.ShareCallOutput.SharedWith,
                )
            )
        )

        result = appsync_session.execute(mutation)

        # Send notification to recipients who no longer have access to the meeting
        if unshare_list:
            input = {"CallId": callid, "SharedWith": unshare_list}
            mutation = dsl_gql(
                DSLMutation(
                    schema.Mutation.unshareCall.args(input=input).select(
                        schema.UnshareCallOutput.CallId, schema.UnshareCallOutput.SharedWith
                    )
                )
            )

            appsync_session.execute(mutation)

    except ClientError as err:
        logger.error(
            "Error updating people can access %s: %s",
            err.response["Error"]["Code"],
            err.response["Error"]["Message"],
        )
        raise
    else:
        return


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


def share_keyframes(callid, new_recipients):
    """Give the meeting's key frames the meeting's new SharedWith.

    Key frames follow the transcript's access rule, so they are shared and
    unshared with it. Written directly rather than through AppSync: there is no
    mutation for it, and the function's role already allows UpdateItem on the
    table.
    """
    updated = 0
    for key in _keyframe_keys(callid):
        if new_recipients:
            ddbTable.update_item(
                Key=key,
                UpdateExpression="SET SharedWith = :val",
                ExpressionAttributeValues={":val": new_recipients},
            )
        else:
            ddbTable.update_item(Key=key, UpdateExpression="REMOVE SharedWith")
        updated += 1
    return updated


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


def share_chat_attachments(callid, new_recipients):
    """Give the meeting's chat attachments the meeting's new SharedWith, as its key frames."""
    updated = 0
    for key in _chat_attachment_keys(callid):
        if new_recipients:
            ddbTable.update_item(
                Key=key,
                UpdateExpression="SET SharedWith = :val",
                ExpressionAttributeValues={":val": new_recipients},
            )
        else:
            ddbTable.update_item(Key=key, UpdateExpression="REMOVE SharedWith")
        updated += 1
    return updated


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


def update_transcript_segment(appsync_session, schema, PK, SK, new_recipients):
    input = {"PK": PK, "SK": SK, "SharedWith": new_recipients}

    try:
        mutation = dsl_gql(
            DSLMutation(
                schema.Mutation.shareTranscriptSegment.args(input=input).select(
                    schema.ShareTranscriptSegmentOutput.PK,
                )
            )
        )
        appsync_session.execute(mutation)

    except ClientError as err:
        logger.error(
            "Error updating transcript segment %s: %s",
            err.response["Error"]["Code"],
            err.response["Error"]["Message"],
        )
        raise
    else:
        return


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


def _auto_share_enabled() -> bool:
    """Meeting Sharing › Share meetings with their listed participants; on unless saved off."""
    try:
        from lma_config import read_setting  # configuration layer
    except ImportError:  # pragma: no cover - the layer is attached in every deployment
        logger.warning(
            "Configuration layer not available; using the shipped Meeting Sharing default"
        )
        return True
    return read_setting("meeting_sharing", "shareWithListedParticipants", default=True) is not False


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


def _sharing_write(call_id: str, item: Mapping[str, Any], plan, recipients, list_key) -> list:
    """The transaction that records a plan: SharedWith and AutoSharedWith on the meeting, and
    SharedWith on its list row, only if the meeting's SharedWith is still what was read."""
    pk = f"c#{call_id}"
    names = {"#auto": auto_share.AUTO_SHARED_ATTRIBUTE, "#sw": "SharedWith"}
    values: dict = {}
    sets, removes = [], []
    if recipients is not None:
        if recipients:
            sets.append("#sw = :sw")
            values[":sw"] = recipients
        else:
            removes.append("#sw")
    if plan.auto_shared:
        sets.append("#auto = :auto")
        values[":auto"] = plan.auto_shared
    else:
        removes.append("#auto")
    expression = " ".join(
        part
        for part in (
            f"SET {', '.join(sets)}" if sets else "",
            f"REMOVE {', '.join(removes)}" if removes else "",
        )
        if part
    )
    # The meeting must still exist, and hold the SharedWith the plan was made from:
    # a share the owner makes meanwhile in the share dialog is not overwritten.
    condition = "attribute_exists(PK) AND "
    if item.get("SharedWith") is None:
        condition += "attribute_not_exists(#sw)"
    else:
        condition += "#sw = :seen"
        values[":seen"] = item["SharedWith"]
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
            "ConditionExpression": "attribute_exists(PK)",
            "ExpressionAttributeNames": {"#sw": "SharedWith"},
        }
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


def apply_auto_share(call_id: str, item: Mapping[str, Any], plan: auto_share.AutoSharePlan) -> None:
    """Record the plan, then give the meeting's items the new sharing list.

    The meeting's SharedWith and AutoSharedWith, and its list row's SharedWith,
    are written in one transaction, on condition that the meeting's SharedWith is
    still the value the plan was made from; if the owner changed it meanwhile
    (the share dialog), SharingChanged is raised and the record is retried, so
    the plan is made again from the owner's change. Then, as share_meeting does
    for the share dialog, the transcript, key frames and chat attachments are
    given the list, and anyone unshared is told; that is repeated once with the
    meeting's SharedWith as it is afterwards, should another share have landed
    while the items were being written.
    """
    recipients = None
    list_key = None
    if plan.changes_sharing:
        list_key = auto_share.list_row_key(call_id, item.get("CreatedAt"))
        if list_key is None:
            raise ValueError(f"meeting {call_id} has no CreatedAt to find its list row by")
        recipients = auto_share.shared_with_after(shared_with_entries(item.get("SharedWith")), plan)
    try:
        _transact(_sharing_write(call_id, item, plan, recipients, list_key))
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = [r.get("Code") for r in err.response.get("CancellationReasons") or []]
        pk = f"c#{call_id}"
        if not ddbTable.get_item(Key={"PK": pk, "SK": pk}, ConsistentRead=True).get("Item"):
            logger.info("Meeting %s was deleted before its shares were recorded", call_id)
            return
        raise SharingChanged(f"the sharing of {call_id} changed while it was planned ({reasons})")
    if recipients is None:
        return
    with appsync_client as appsync_session:
        if not appsync_session.client.schema:
            raise ValueError("invalid AppSync schema")
        schema = DSLSchema(appsync_session.client.schema)
        _share_items(appsync_session, schema, call_id, recipients)
        pk = f"c#{call_id}"
        current = ddbTable.get_item(Key={"PK": pk, "SK": pk}, ConsistentRead=True).get("Item") or {}
        now = shared_with_entries(current.get("SharedWith"))
        if current and sorted(now) != sorted(recipients):
            _share_items(appsync_session, schema, call_id, now)
        if plan.remove:
            _tell_unshared(appsync_session, schema, call_id, list(plan.remove))


def _tell_unshared(appsync_session, schema, call_id: str, removed: list) -> None:
    """unshareCall, which tells the people a meeting is no longer shared with, as share_meeting does."""
    mutation = dsl_gql(
        DSLMutation(
            schema.Mutation.unshareCall.args(
                input={"CallId": call_id, "SharedWith": removed}
            ).select(schema.UnshareCallOutput.CallId, schema.UnshareCallOutput.SharedWith)
        )
    )
    appsync_session.execute(mutation)


def _share_items(appsync_session, schema, call_id: str, recipients: list) -> None:
    """The meeting's transcript segments, key frames and chat attachments, given ``recipients``."""
    new_recipients = list(recipients) or None
    for segment in get_transcript_segments(appsync_session, schema, call_id):
        update_transcript_segment(
            appsync_session, schema, segment["PK"], segment["SK"], new_recipients
        )
    share_keyframes(call_id, new_recipients)
    share_chat_attachments(call_id, new_recipients)


def handle_meeting_stream(event: Mapping[str, Any]) -> dict:
    """The meetings table's stream: share each changed meeting with its listed participants.

    The event source mapping passes only writes of items whose PK starts c#;
    of those, only meeting records (PK equal to SK) are read, and each meeting
    once per batch, from its latest record. The record in the stream decides
    whether anything is to be done, without a read; when something is, the
    meeting is read again, consistently, and planned from what it holds now,
    since later writes of the same meeting can follow in the stream. A
    meeting whose plan fails is reported, so the batch is retried from it.
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
    failures = []
    enabled = None
    changed = 0
    for call_id, record in latest.items():
        try:
            if not _auto_share_plan(_stream_image(record)).changes_anything:
                continue
            if enabled is None:
                enabled = _auto_share_enabled()
            if not enabled:
                continue
            pk = f"c#{call_id}"
            item = ddbTable.get_item(Key={"PK": pk, "SK": pk}, ConsistentRead=True).get("Item")
            if not item:
                continue
            plan = _auto_share_plan(item)
            if not plan.changes_anything:
                continue
            apply_auto_share(call_id, item, plan)
            changed += 1
            # Counts only: the addresses are personal data.
            logger.info(
                "Meeting sharing for %s: %d added, %d removed",
                call_id,
                len(plan.add),
                len(plan.remove),
            )
        except Exception:  # noqa: BLE001 - reported for retry, the batch goes on
            logger.exception("Meeting sharing failed for %s", call_id)
            failures.append(
                {"itemIdentifier": (record.get("dynamodb") or {}).get("SequenceNumber", "")}
            )
    return {"batchItemFailures": failures}


def lambda_handler(event, context):
    # The meetings table's stream (Meeting Sharing), rather than an AppSync request.
    if "Records" in event:
        return handle_meeting_stream(event)
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
        return share_meetings(calls, owner, recipients)
    elif action == "deleteMeetings":
        return delete_meetings(calls, owner)
    else:
        return {"Result": "Invalid action"}
