import os
import io
import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
import json
import csv
import logging
import re

# grab environment variables
LCA_CALL_EVENTS_TABLE = os.environ['LCA_CALL_EVENTS_TABLE']

logger = logging.getLogger(__name__)
ddb = boto3.resource('dynamodb')
ddbTable = ddb.Table(LCA_CALL_EVENTS_TABLE)

def update_meeting_permissions(callid, listPK, listSK, recipients):
    pk = 'c#' + callid
    new_recipients = list(set(email.strip() for email in recipients.split(',') if email.strip()))

    try:
        response = ddbTable.get_item(
            Key={'PK': pk, 'SK': pk},
            ProjectionExpression='SharedWith'
        )
        
        current_recipients = response.get('Item', {}).get('SharedWith', [])

        if set(new_recipients).issubset(set(current_recipients)):
            print(f"Recipients already have access for {callid}")
            return { 'Result': f"No update needed for CallId: {callid}" }
        
        combined_recipients = list(set(current_recipients + new_recipients))

        updated_count = update_recipients(pk, combined_recipients, None)
        
        pk = 'trs#' + callid
        updated_count += update_recipients(pk, combined_recipients, None)

        updated_count += update_recipients(listPK, combined_recipients, listSK)

        print(f"Successfully updated {updated_count} items for CallId: {callid}")
        return

    except ClientError as err:
        logger.error("Error updating people can access %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return

def update_recipients(pk, recipients, sk):
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
            ddbTable.update_item(
                Key={
                    'PK': item['PK'],
                    'SK': item['SK']
                },
                UpdateExpression="SET SharedWith = :val",
                ExpressionAttributeValues={':val': recipients}
            )
            updated_count += 1

    except ClientError as err:
        logger.error("Error updating recipients for %s: %s",
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

def get_transcripts(callid):
    pk = 'trs#'+callid
    print(f"Call Transcript PK: {pk}")

    try:
        response = ddbTable.query(KeyConditionExpression=Key('PK').eq(pk), FilterExpression=(
            Attr('Channel').eq('AGENT') | Attr('Channel').eq('CALLER')) & Attr('IsPartial').eq(False))
        # response = ddbTable.query(KeyConditionExpression=Key('PK').eq(pk))
    except ClientError as err:
        logger.error("Error getting transcripts from LCA Call Events table %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        # print(response['Items'])
        return response['Items']

def lambda_handler(event, context):
    print("Received event: " + json.dumps(event))

    request_owner = event["identity"]["username"]
    isAdminUser = False
    groups = event["identity"].get("groups")
    if groups:
        isAdminUser = "Admin" in groups       

    calls = event["arguments"]["input"]["Calls"]
    recipients = event["arguments"]["input"]["MeetingRecipients"]

    if not recipients:
        return { 'Result': "No recipients provided" }
    if not all(re.match(r"[^@]+@[^@]+\.[^@]+", email) for email in recipients.split(', ')):
        return { 'Result': "Invalid email address provided" }

    for call in calls:        
        callid = call['CallId']
        listPK = call['ListPK']
        listSK = call['ListSK']

        print("CallID: ", callid, listPK, listSK)
        transcripts = get_transcripts(callid)
        metadata = get_call_metadata(callid)
        print("Fetch Transcript response:", metadata)
        if(metadata.get('Owner', '') != request_owner and not isAdminUser):
            return { 'Result': "You don't have permission to share one or more of the requested calls" }

        update_meeting_permissions(callid, listPK, listSK, recipients)
        
    return { 'Result': "Meetings shared successfully" }

# Test case
if __name__ == '__main__':
    lambda_handler({
        "CallId": "2359fb61-f612-4fe9-bce2-839061c328f9",
        "TokenCount": 0,
        "ProcessTranscript": False,
        "LastNTurns": 20
    }, {})
