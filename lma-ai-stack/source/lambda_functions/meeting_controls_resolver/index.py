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

import boto3
from appsync_utils import AppsyncRequestsGqlClient
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from gql import gql  # noqa: F401
from gql.dsl import DSLMutation, DSLQuery, DSLSchema, dsl_gql

# Concurrency tuning.  AppSync + DynamoDB easily absorb dozens of in-flight
# requests per meeting, but we cap both dials so a huge bulk delete can't
# saturate the default requests connection pool (~10 connections) or DynamoDB
# per-partition WCU on-demand limits.
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
VP_TABLE_NAME = environ.get("VP_TABLE_NAME")
VP_TASK_REGISTRY_TABLE_NAME = environ.get("VP_TASK_REGISTRY_TABLE_NAME")
KINESIS_STREAM_NAME = environ.get("KINESIS_STREAM_NAME")

logger = logging.getLogger(__name__)
ddb = boto3.resource("dynamodb")
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
    try:
        query = dsl_gql(
            DSLQuery(
                schema.Query.getTranscriptSegments.args(callId=callid).select(
                    schema.TranscriptSegmentList.TranscriptSegments.select(
                        schema.TranscriptSegment.PK,
                        schema.TranscriptSegment.SK,
                        schema.TranscriptSegment.CreatedAt,
                        schema.TranscriptSegment.CallId,
                        schema.TranscriptSegment.SegmentId,
                        schema.TranscriptSegment.StartTime,
                        schema.TranscriptSegment.EndTime,
                        schema.TranscriptSegment.Transcript,
                        schema.TranscriptSegment.IsPartial,
                        schema.TranscriptSegment.Channel,
                        schema.TranscriptSegment.Speaker,
                    )
                )
            )
        )

        result = appsync_session.execute(query)
    except ClientError as err:
        logger.error(
            "Error deleting meetings %s: %s",
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


def share_meetings(calls, owner, recipients):
    if recipients and not all(
        re.match(r"[^@]+@[^@]+\.[^@]+", email) for email in recipients.split(",")
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
        result = get_transcript_segments(appsync_session, schema, callid)
        for transcript_segment in result.get("getTranscriptSegments").get("TranscriptSegments"):
            update_transcript_segment(
                appsync_session,
                schema,
                transcript_segment["PK"],
                transcript_segment["SK"],
                new_recipients,
            )

        input = {
            "CallId": callid,
            "ListPK": listPK,
            "ListSK": listSK,
            "Owner": owner,
            "SharedWith": new_recipients,
        }

        result = get_call_details(appsync_session, schema, callid)
        shared_with = result.get("getCall").get("SharedWith")

        if shared_with:
            shared_with = [recipient.strip() for recipient in shared_with[1:-1].split(",")]

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

        # Expand the underlying requests pool so MEETING_CONCURRENCY *
        # SEGMENT_CONCURRENCY concurrent calls don't serialise on the default
        # 10-connection limit.
        _tune_requests_pool(appsync_session, MEETING_CONCURRENCY * SEGMENT_CONCURRENCY + 4)

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
        # Run the three independent preparation steps in parallel:
        #  1. Virtual Participant cleanup (scan + ECS/DDB writes)
        #  2. Fetch + bulk-delete transcript segments via AppSync
        #  3. Fetch call details (needed to preserve SharedWith on deleteCall)
        # Plus start S3 prefix deletion as soon as we kick off steps 1-3.
        with ThreadPoolExecutor(max_workers=4) as pool:
            vp_future = pool.submit(cleanup_virtual_participants, callid)
            segments_future = pool.submit(
                _delete_all_transcript_segments, appsync_session, schema, callid
            )
            details_future = pool.submit(get_call_details, appsync_session, schema, callid)
            # S3 cleanup is independent of AppSync/DDB state, so start it now.
            s3_future = pool.submit(delete_recordings_transcripts, callid)

            # Surface any exception from the parallel preparation steps.
            vp_future.result()
            segments_future.result()
            details_result = details_future.result()
            s3_future.result()

        shared_with = (
            details_result.get("getCall", {}).get("SharedWith") if details_result else None
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


def _delete_all_transcript_segments(appsync_session, schema, callid):
    """Fetch every transcript segment for the meeting and delete them in
    parallel via ``deleteTranscriptSegment`` mutations.
    """
    result = get_transcript_segments(appsync_session, schema, callid)
    segments = (result or {}).get("getTranscriptSegments", {}).get("TranscriptSegments") or []
    if not segments:
        return

    workers = max(1, min(SEGMENT_CONCURRENCY, len(segments)))
    first_exc = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                delete_transcript_segment,
                appsync_session,
                schema,
                seg["PK"],
                seg["SK"],
            )
            for seg in segments
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.error("Error deleting a transcript segment for %s: %s", callid, exc)
                if first_exc is None:
                    first_exc = exc
    if first_exc is not None:
        raise first_exc


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


def delete_transcript_segment(appsync_session, schema, PK, SK):
    input = {
        "PK": PK,
        "SK": SK,
    }

    try:
        mutation = dsl_gql(
            DSLMutation(
                schema.Mutation.deleteTranscriptSegment.args(input=input).select(
                    schema.DeleteTranscriptSegmentOutput.CallId,
                )
            )
        )

        appsync_session.execute(mutation)

    except ClientError as err:
        logger.error(
            "Error deleting transcript segment %s: %s",
            err.response["Error"]["Code"],
            err.response["Error"]["Message"],
        )
        raise
    else:
        return


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
