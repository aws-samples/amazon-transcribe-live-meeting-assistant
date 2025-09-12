#
# 
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. 
# SPDX-License-Identifier: MIT-0
# 
#
import os
from os import environ
import io
from urllib.parse import urlparse
import ast
import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
import json
import logging
import re
from gql import gql, Client
from gql.dsl import DSLMutation, DSLSchema, DSLQuery, dsl_gql
from appsync_utils import AppsyncRequestsGqlClient
from datetime import datetime, timezone

APPSYNC_GRAPHQL_URL = environ["APPSYNC_GRAPHQL_URL"]
appsync_client = AppsyncRequestsGqlClient(
    url=APPSYNC_GRAPHQL_URL, fetch_schema_from_transport=True)

# grab environment variables
LCA_CALL_EVENTS_TABLE = environ['LCA_CALL_EVENTS_TABLE']
S3_BUCKET_NAME = environ['S3_BUCKET_NAME']
S3_RECORDINGS_PREFIX = environ['S3_RECORDINGS_PREFIX']
S3_TRANSCRIPTS_PREFIX = environ['S3_TRANSCRIPTS_PREFIX']
VP_TABLE_NAME = environ.get('VP_TABLE_NAME')
VP_TASK_REGISTRY_TABLE_NAME = environ.get('VP_TASK_REGISTRY_TABLE_NAME')
KINESIS_STREAM_NAME = environ.get('KINESIS_STREAM_NAME')

logger = logging.getLogger(__name__)
ddb = boto3.resource('dynamodb')
ddbTable = ddb.Table(LCA_CALL_EVENTS_TABLE)
s3_client = boto3.client('s3')
kinesis_client = boto3.client('kinesis')
ecs_client = boto3.client('ecs')

### Common functions

def posixify_filename(filename: str) -> str:
    # Replace all invalid characters with underscores
    regex = r'[^a-zA-Z0-9_.]'
    posix_filename = re.sub(regex, '_', filename)
    # Remove leading and trailing underscores
    posix_filename = re.sub(r'^_+', '', posix_filename)
    posix_filename = re.sub(r'_+$', '', posix_filename)
    return posix_filename

def delete_recordings_transcripts(callid):
    filename = posixify_filename(f"{callid}")
    prefix = f"{S3_RECORDINGS_PREFIX}{filename}"

    response = s3_client.list_objects_v2(
        Bucket=S3_BUCKET_NAME,
        Prefix=prefix
    )
    if 'Contents' in response:
        for object in response['Contents']:
            print('Deleting ', object['Key'])
            response = s3_client.delete_object(
                Bucket=S3_BUCKET_NAME,
                Key=object['Key']
            )

    prefix = f"{S3_TRANSCRIPTS_PREFIX}{filename}"
    response = s3_client.list_objects_v2(
        Bucket=S3_BUCKET_NAME,
        Prefix=prefix
    )

    if 'Contents' in response:
        for object in response['Contents']:
            print('Deleting ', object['Key'])
            response = s3_client.delete_object(
                Bucket=S3_BUCKET_NAME,
                Key=object['Key']
            )

    return

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
        logger.error("Error getting call details %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
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
        logger.error("Error deleting meetings %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return result

def get_call_metadata(callid):
    pk = 'c#'+callid
    try:
        metadata = ddbTable.get_item(
            Key={'PK': pk, 'SK': pk},
            TableName=LCA_CALL_EVENTS_TABLE
            )
    except ClientError as err:
        logger.error("Error getting metadata from LCA Call Events table %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return metadata['Item']

def verify_permissions(event):
    request_owner = event["identity"]["username"]
    isAdminUser = False
    groups = event["identity"].get("groups")
    if groups:
        isAdminUser = "Admin" in groups

    calls = event["arguments"]["input"]["Calls"]
    for call in calls:
        callid = call['CallId']
        listPK = call['ListPK']
        listSK = call['ListSK']

        metadata = get_call_metadata(callid)
        if(metadata.get('Owner', '') != request_owner and not isAdminUser):
            return False

    return True

## Share Meetings

def share_meetings(calls, owner, recipients):
    if recipients and not all(re.match(r"[^@]+@[^@]+\.[^@]+", email) for email in recipients.split(',')):
        return { 'Result': "Invalid email address provided" }

    with appsync_client as appsync_session:
        if not appsync_session.client.schema:
            raise ValueError("invalid AppSync schema")
        schema = DSLSchema(appsync_session.client.schema)

        for call in calls:
            callid = call['CallId']
            listPK = call['ListPK']
            listSK = call['ListSK']
            share_meeting(appsync_session, schema, callid, listPK, listSK, recipients, owner)
    
    callIds = [call['CallId'] for call in calls]
    return { 'Calls': callIds, 
             'Result': "Meetings shared successfully",
             'Owner': owner,
             'SharedWith': recipients 
        }

def share_meeting(appsync_session, schema, callid, listPK, listSK, recipients, owner):
    new_recipients = list(set(email.strip() for email in recipients.split(',') if email.strip()))

    if not recipients:
        new_recipients = None

    try:        
        result = get_transcript_segments(appsync_session, schema, callid)
        for transcript_segment in result.get("getTranscriptSegments").get("TranscriptSegments"):
            update_transcript_segment(appsync_session, schema, transcript_segment["PK"], transcript_segment["SK"], new_recipients)

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
            shared_with = [recipient.strip() for recipient in shared_with[1:-1].split(',')]

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
                    schema.ShareCallOutput.SharedWith
                )
            )
        )

        result = appsync_session.execute(mutation)

        # Send notification to recipients who no longer have access to the meeting
        if unshare_list:
            input = {
                "CallId": callid,
                "SharedWith": unshare_list
            }
            mutation = dsl_gql(
                DSLMutation(
                    schema.Mutation.unshareCall.args(input=input).select(
                        schema.UnshareCallOutput.CallId,
                        schema.UnshareCallOutput.SharedWith
                    )
                )
            )

            result = appsync_session.execute(mutation)

    except ClientError as err:
        logger.error("Error updating people can access %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return

def update_transcript_segment(appsync_session, schema, PK, SK, new_recipients):
    input = {
        "PK": PK,
        "SK": SK,
        "SharedWith": new_recipients
    }

    try:
        mutation = dsl_gql(
            DSLMutation(
                schema.Mutation.shareTranscriptSegment.args(input=input).select(
                    schema.ShareTranscriptSegmentOutput.PK,
                )
            )
        )
        result = appsync_session.execute(mutation)

    except ClientError as err:
        logger.error("Error updating transcript segment %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return

## Delete Meetings

def delete_meetings(calls, owner):
    with appsync_client as appsync_session:
        if not appsync_session.client.schema:
            raise ValueError("invalid AppSync schema")
        schema = DSLSchema(appsync_session.client.schema)

        for call in calls:        
            callid = call['CallId']
            listPK = call['ListPK']
            listSK = call['ListSK']
            delete_meeting(appsync_session, schema, callid, listPK, listSK, owner)

    return { 'Result': "Meetings deleted successfully" }

def delete_meeting(appsync_session, schema, callid, listPK, listSK, owner):
    try:
        # First check and cleanup any associated Virtual Participants
        cleanup_virtual_participants(callid)
        
        # Delete the transcript segments
        result = get_transcript_segments(appsync_session, schema, callid)

        for transcript_segment in result.get("getTranscriptSegments").get("TranscriptSegments"):
            delete_transcript_segment(appsync_session, schema, transcript_segment["PK"], transcript_segment["SK"])

        result = get_call_details(appsync_session, schema, callid)
        shared_with = result.get("getCall").get("SharedWith")

        delete_recordings_transcripts(callid)

        input = {
            "CallId": callid,
            "ListPK": listPK,
            "ListSK": listSK,
            "Owner": owner,
            "SharedWith": shared_with,
        }

        # Now delete the call records (PK that begins with c# and cls#)
        mutation = dsl_gql(
            DSLMutation(
                schema.Mutation.deleteCall.args(input=input).select(
                    schema.DeleteCallOutput.CallId,
                    schema.DeleteCallOutput.Owner,
                    schema.DeleteCallOutput.SharedWith
                )
            )
        )

        result = appsync_session.execute(mutation)
    except ClientError as err:
        logger.error("Error deleting meetings %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return

def cleanup_virtual_participants(callid):
    """Clean up Virtual Participants associated with the meeting being deleted"""
    if not VP_TABLE_NAME:
        logger.info("VP_TABLE_NAME not configured, skipping VP cleanup")
        return
    
    try:
        vp_table = ddb.Table(VP_TABLE_NAME)
        
        # Scan for VPs with matching CallId
        response = vp_table.scan(
            FilterExpression=Attr('CallId').eq(callid)
        )
        
        vps_to_cleanup = response.get('Items', [])
        
        for vp in vps_to_cleanup:
            vp_id = vp.get('id')
            if not vp_id:
                continue
                
            logger.info(f"Cleaning up VP {vp_id} for meeting {callid}")
            
            # Send END event to Kinesis
            send_vp_end_event(callid, vp)
            
            # Stop ECS task if running
            stop_vp_ecs_task(vp_id)
            
            # Delete VP record
            vp_table.delete_item(Key={'id': vp_id})
            
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
            'EventType': 'END',
            'CallId': callid,
            'CustomerPhoneNumber': 'Virtual Participant',
            'SystemPhoneNumber': 'LMA System',
            'CreatedAt': datetime.now(timezone.utc).isoformat(),
            'AgentId': vp.get('owner', 'Unknown'),
            'AccessToken': '',
            'IdToken': '',
            'RefreshToken': '',
        }
        
        kinesis_client.put_record(
            StreamName=KINESIS_STREAM_NAME,
            PartitionKey=callid,
            Data=json.dumps(end_event).encode('utf-8')
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
        response = registry_table.get_item(Key={'vpId': vp_id})
        
        task_details = response.get('Item')
        if not task_details:
            logger.info(f"No task registry entry found for VP {vp_id}")
            return
            
        task_arn = task_details.get('taskArn')
        cluster_arn = task_details.get('clusterArn')
        
        if task_arn and cluster_arn:
            ecs_client.stop_task(
                cluster=cluster_arn,
                task=task_arn,
                reason=f'Meeting deleted - VP {vp_id} cleanup'
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
        registry_table.delete_item(Key={'vpId': vp_id})
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

        result = appsync_session.execute(mutation)

    except ClientError as err:
        logger.error("Error deleting transcript segment %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return

def lambda_handler(event, context):
    owner = event["identity"]["username"]
    if not verify_permissions(event):
        return { 'Result': "You don't have permission to share or delete one or more of the requested calls" }

    calls = event["arguments"]["input"]["Calls"]
    action = event["info"]["fieldName"]

    if(action == "shareMeetings"):
        recipients = event["arguments"]["input"]["MeetingRecipients"]
        return share_meetings(calls, owner, recipients)
    elif(action == "deleteMeetings"):
        return delete_meetings(calls, owner)
    else:
        return { 'Result': "Invalid action" }