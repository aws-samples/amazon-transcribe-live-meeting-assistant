import os
import io
import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
import json
import csv
import logging
import re
from eventprocessor_utils import (
    get_owner_from_jwt,
)


# grab environment variables
LCA_CALL_EVENTS_TABLE = os.environ['LCA_CALL_EVENTS_TABLE']

logger = logging.getLogger(__name__)
ddb = boto3.resource('dynamodb')
ddbTable = ddb.Table(LCA_CALL_EVENTS_TABLE)

def update_meeting_permissions(callid, recipients):
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
            return f"No update needed for CallId: {callid}"
        
        combined_recipients = list(set(current_recipients + new_recipients))

        response = ddbTable.scan(
            FilterExpression=Attr('CallId').eq(callid)
        )
        items = response['Items']
        
        while 'LastEvaluatedKey' in response:
            response = ddbTable.scan(
                FilterExpression=Attr('CallId').eq(callid),
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
                ExpressionAttributeValues={':val': combined_recipients},
            )
            updated_count += 1

        print(f"Successfully updated {updated_count} items for CallId: {callid}")
        return f"Updated {updated_count} items"

        # response = ddbTable.update_item(
        #     Key={'PK': pk, 'SK': pk},
        #     UpdateExpression="SET SharedWith = :val",
        #     ExpressionAttributeValues={':val': combined_recipients},
        #     ReturnValues="UPDATED_NEW"
        # )
        
    except ClientError as err:
        logger.error("Error updating people can access %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return response['Attributes']

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
    print("Received event: " + json.dumps(event, indent=2))
    request_owner = get_owner_from_jwt(event['accessToken'], True)
    print("DECODED JWT", request_owner)


    data = json.loads(json.dumps(event))
    callIds = data['callIds']
    
    for callid in callIds:
        print("CallID: ", callid)
        transcripts = get_transcripts(callid)
        metadata = get_call_metadata(callid)
        print("Fetch Transcript response:", metadata)
        if(metadata.get('Owner', '') != request_owner):
            return "You don't have permission to share one or more of the requested calls"
        update_meeting_permissions(callid, event['meetingRecipients'])
        
    return "Meetings shared successfully"

# Test case
if __name__ == '__main__':
    lambda_handler({
        "CallId": "2359fb61-f612-4fe9-bce2-839061c328f9",
        "TokenCount": 0,
        "ProcessTranscript": False,
        "LastNTurns": 20
    }, {})
