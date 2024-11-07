from os import environ
import io
import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
import json
import logging
import re
from gql import gql, Client
from gql.dsl import DSLMutation, DSLSchema, DSLQuery, dsl_gql
from graphql.language.printer import print_ast
from appsync_utils import AppsyncRequestsGqlClient

APPSYNC_GRAPHQL_URL = environ["APPSYNC_GRAPHQL_URL"]
appsync_client = AppsyncRequestsGqlClient(
    url=APPSYNC_GRAPHQL_URL, fetch_schema_from_transport=True)

# grab environment variables
LCA_CALL_EVENTS_TABLE = environ['LCA_CALL_EVENTS_TABLE']

logger = logging.getLogger(__name__)
ddb = boto3.resource('dynamodb')
ddbTable = ddb.Table(LCA_CALL_EVENTS_TABLE)

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

def delete_meetings(calls, owner):
    with appsync_client as appsync_session:
        if not appsync_session.client.schema:
            raise ValueError("invalid AppSync schema")
        schema = DSLSchema(appsync_session.client.schema)

        for call in calls:        
            callid = call['CallId']
            listPK = call['ListPK']
            listSK = call['ListSK']
            print("CallID: ", callid, listPK, listSK)
            delete_meeting(appsync_session, schema, callid, listPK, listSK, owner)

    return { 'Result': "Meetings deleted successfully" }

def delete_meeting(appsync_session, schema, callid, listPK, listSK, owner):
    input = {
        "CallId": callid,
        "ListPK": listPK,
        "ListSK": listSK,
        "Owner": owner,
    }
    try:
        # First delete the transcript segments
        result = get_transcript_segments(appsync_session, schema, callid)

        for transcript_segment in result.get("getTranscriptSegments").get("TranscriptSegments"):
            delete_transcript_segment(appsync_session, schema, transcript_segment["PK"], transcript_segment["SK"])

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
        print("delete query result", result)
    except ClientError as err:
        logger.error("Error deleting meetings %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return

def delete_transcript_segment(appsync_session, schema, PK, SK):
    input = {
        "PK": PK,
        "SK": SK,
    }
    print("Trs segment args", schema.input.DeleteTranscriptSegmentInput)

    try:
        mutation = dsl_gql(
            DSLMutation(
                schema.Mutation.deleteTranscriptSegment.args(input=input).select(
                    schema.DeleteTranscriptSegmentOutput.CallId,
                )
            )
        )

        result = appsync_session.execute(mutation)
        print("delete transcript segment result", result)

    except ClientError as err:
        logger.error("Error deleting transcript segment %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return

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
        query_string = print_ast(query)
        print("get transcript segments result", query_string, result)
    except ClientError as err:
        logger.error("Error deleting meetings %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return result

def update_meeting_permissions(callid, listPK, listSK, recipients):
    pk = 'c#' + callid
    new_recipients = list(set(email.strip() for email in recipients.split(',') if email.strip()))

    if not recipients:
        new_recipients = None
    try:        
        updated_count = ddb_update_delete(pk, None, 'Share', new_recipients )

        pk = 'trs#' + callid
        updated_count += ddb_update_delete(pk, None, 'Share', new_recipients )

        updated_count += ddb_update_delete(listPK, listSK, 'Share', new_recipients)

        print(f"Successfully updated {updated_count} items for CallId: {callid}")
        return

    except ClientError as err:
        logger.error("Error updating people can access %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return

def ddb_update_delete(pk, sk, action, recipients ):
    try:
        if (sk):
            items = [
                {
                    'PK': pk,
                    'SK': sk
                }
            ]
        else:
            response = ddbTable.query(
                KeyConditionExpression=Key('PK').eq(pk)
            )
            items = response['Items']
            
            while 'LastEvaluatedKey' in response:
                response = ddbTable.query(
                    KeyConditionExpression=Key('PK').eq(pk),
                    ExclusiveStartKey=response['LastEvaluatedKey']
                )
                items.extend(response['Items'])

        updated_count = 0
        for item in items:
            if action == 'Share':
                ddbTable.update_item(
                    Key={
                        'PK': item['PK'],
                        'SK': item['SK']
                    },
                    UpdateExpression="SET SharedWith = :val",
                    ExpressionAttributeValues={':val': recipients}
                )
            elif action == 'Delete':
                ddbTable.delete_item(
                    Key={
                        'PK': item['PK'],
                        'SK': item['SK']
                    }
                )
            updated_count += 1
    except ClientError as err:
        logger.error("Error during update or delete for %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return updated_count

def get_call_metadata(callid):
    pk = 'c#'+callid
    print(f"Call metadata PK: {pk}")
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

def share_meetings(calls, owner, recipients):
    if recipients and not all(re.match(r"[^@]+@[^@]+\.[^@]+", email) for email in recipients.split(',')):
        return { 'Result': "Invalid email address provided" }

    for call in calls:        
        callid = call['CallId']
        listPK = call['ListPK']
        listSK = call['ListSK']
        print("CallID: ", callid, listPK, listSK)
        update_meeting_permissions(callid, listPK, listSK, recipients)
    
    callIds = [call['CallId'] for call in calls]
    return { 'Calls': callIds, 
             'Result': "Meetings shared successfully",
             'Owner': owner,
             'SharedWith': recipients 
        }

def lambda_handler(event, context):
    print("Received event: " + json.dumps(event))
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

# Test case
if __name__ == '__main__':
    lambda_handler({
        "CallId": "2359fb61-f612-4fe9-bce2-839061c328f9",
        "TokenCount": 0,
        "ProcessTranscript": False,
        "LastNTurns": 20
    }, {})
