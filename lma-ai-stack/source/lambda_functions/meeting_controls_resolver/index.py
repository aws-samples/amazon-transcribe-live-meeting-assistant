# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from os import environ
from urllib.parse import unquote, urlparse

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


def _batch_delete_prefix(bucket: str, prefix: str) -> int:
    """List every key under ``prefix`` and delete them in ``DeleteObjects``
    batches of up to 1000 keys.  Returns the number of keys deleted.

    This is ~1 round-trip per 1000 keys versus the old 1 request per key,
    so a meeting with dozens of recording chunks / transcript files goes
    from dozens of round-trips to one.
    """
    deleted = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    batch: list[dict] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            batch.append({"Key": obj["Key"]})
            if len(batch) >= S3_BATCH_DELETE_LIMIT:
                s3_client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
                deleted += len(batch)
                batch = []
    if batch:
        s3_client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        deleted += len(batch)
    return deleted


def delete_recordings_transcripts(callid):
    filename = posixify_filename(f"{callid}")
    recordings_prefix = f"{S3_RECORDINGS_PREFIX}{filename}"
    transcripts_prefix = f"{S3_TRANSCRIPTS_PREFIX}{filename}"

    # Run the two prefix-scans in parallel: they hit different key spaces
    # (recordings vs transcripts) so there's no contention.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_batch_delete_prefix, S3_BUCKET_NAME, recordings_prefix): "recordings",
            pool.submit(_batch_delete_prefix, S3_BUCKET_NAME, transcripts_prefix): "transcripts",
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
    """Delete one recording file by key; 1 if S3 reported no error, else 0.

    S3 reports no error for a key that is already gone, so 1 means the file is
    not there now, not that this call removed it: a transcriber recording named
    by RecordingUrl has usually been removed already by the CallId-prefix scan.
    """
    response = s3_client.delete_objects(
        Bucket=S3_BUCKET_NAME, Delete={"Objects": [{"Key": key}], "Quiet": True}
    )
    errors = response.get("Errors") or []
    for error in errors:
        logger.error(
            "Could not delete %s recording %s of %s: %s",
            kind,
            error.get("Key"),
            callid,
            error.get("Code"),
        )
    return 0 if errors else 1


def delete_audio_recording(callid, audio_url=None):
    """Delete the audio recording file the meeting record's RecordingUrl names.

    delete_recordings_transcripts removes the files whose names begin with the
    meeting's CallId, which is how the transcriber names its recordings. The
    Virtual Participant names its file from the meeting name and the time the
    recording was written, so that file is found only through the URL stored on
    the meeting. Returns 1 when the file is gone afterwards and 0 otherwise (see
    _delete_stored_recording).
    """
    if not callid:
        return 0
    stored = _recording_key_from_url(audio_url, S3_RECORDINGS_PREFIX)
    if not stored:
        return 0
    deleted = _delete_stored_recording(stored, callid, "audio")
    if deleted:
        logger.info("Deleted audio recording %s for %s", stored, callid)
    return deleted


def delete_video_recordings(callid, video_url=None):
    """Delete the meeting's video recording and any leftover recording segments.

    Two keys are known exactly, so only those are deleted: the file the meeting
    record's VideoRecordingUrl names, and the Virtual Participant's segments
    under ``<chunks prefix><CallId>/``, a folder named with the CallId as it is
    (the trailing slash keeps one meeting's folder from matching another whose
    id merely begins the same way). The video file names themselves are not
    searched: both producers turn the CallId into a file name by replacing
    characters, so two meetings can share one, and a name is no proof of whose
    file it is. Returns the number of recording files deleted (0 or 1).
    """
    if not callid:
        return 0
    deleted = 0
    stored = _video_key_from_url(video_url)
    if stored:
        deleted = _delete_stored_recording(stored, callid, "video")
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
        #  1. Virtual Participant cleanup (scan + ECS/DDB writes)
        #  2. Deletion of every item of the meeting's transcript (PK trs#<CallId>)
        #  3. Fetch call details (needed to preserve SharedWith on deleteCall)
        #  4. S3 prefix deletion
        #  5. Deletion of the meeting's metering records (PK mtr#<CallId>)
        #  6. Deletion of the meeting's key frames (PK kf#<CallId>, and images)
        #  7. Deletion of the meeting's consent evidence images
        with ThreadPoolExecutor(max_workers=7) as pool:
            vp_future = pool.submit(cleanup_virtual_participants, callid)
            segments_future = pool.submit(delete_transcript_items, callid)
            details_future = pool.submit(get_call_details, appsync_session, schema, callid)
            # S3 cleanup is independent of AppSync/DDB state, so start it now.
            s3_future = pool.submit(delete_recordings_transcripts, callid)
            metering_future = pool.submit(delete_metering_records, callid)
            keyframes_future = pool.submit(delete_keyframes, callid)
            evidence_future = pool.submit(delete_consent_evidence, callid)

            # Surface any exception from the parallel preparation steps.
            vp_future.result()
            segments_future.result()
            details_result = details_future.result()
            s3_future.result()
            metering_future.result()
            keyframes_future.result()
            evidence_future.result()

        call_details = (details_result or {}).get("getCall") or {}
        shared_with = call_details.get("SharedWith")

        # After the details, because the stored RecordingUrl and VideoRecordingUrl
        # name the files.
        delete_audio_recording(callid, call_details.get("RecordingUrl"))
        delete_video_recordings(callid, call_details.get("VideoRecordingUrl"))

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
    pk = f"mtr#{callid}"
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
        logger.info("Deleted %d metering record(s) for %s", deleted, callid)
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


def cleanup_virtual_participants(callid):
    """Clean up Virtual Participants associated with the meeting being deleted"""
    if not VP_TABLE_NAME:
        logger.info("VP_TABLE_NAME not configured, skipping VP cleanup")
        return

    try:
        vp_table = ddb.Table(VP_TABLE_NAME)

        # Scan for VPs with matching CallId
        response = vp_table.scan(FilterExpression=Attr("CallId").eq(callid))

        vps_to_cleanup = response.get("Items", [])

        for vp in vps_to_cleanup:
            vp_id = vp.get("id")
            if not vp_id:
                continue

            logger.info(f"Cleaning up VP {vp_id} for meeting {callid}")

            # Send END event to Kinesis
            send_vp_end_event(callid, vp)

            # Stop ECS task if running
            stop_vp_ecs_task(vp_id)

            # Delete VP record
            vp_table.delete_item(Key={"id": vp_id})

            # Delete registry entry
            delete_vp_registry_entry(vp_id)

        if vps_to_cleanup:
            logger.info(f"Cleaned up {len(vps_to_cleanup)} VPs for meeting {callid}")

    except Exception as e:
        logger.error(f"Error cleaning up VPs for meeting {callid}: {e}")


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


def lambda_handler(event, context):
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
