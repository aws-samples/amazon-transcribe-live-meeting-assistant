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

def delete_meeting(callid, listPK, listSK):
    pk = 'c#' + callid
    try:
        deleted_count = ddb_update_delete(pk, None, 'Delete', None)
        pk = 'trs#' + callid
        deleted_count += ddb_update_delete(pk, None, 'Delete', None)
        deleted_count += ddb_update_delete(listPK, listSK, 'Delete', None)
    except ClientError as err:
        logger.error("Error deleting meetings %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        return

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

def share_meetings(calls, recipients):
    if recipients and not all(re.match(r"[^@]+@[^@]+\.[^@]+", email) for email in recipients.split(',')):
        return { 'Result': "Invalid email address provided" }

    for call in calls:        
        callid = call['CallId']
        listPK = call['ListPK']
        listSK = call['ListSK']
        print("CallID: ", callid, listPK, listSK)
        update_meeting_permissions(callid, listPK, listSK, recipients)
    
    return { 'Result': "Meetings shared successfully" }

def delete_meetings(calls):
    for call in calls:        
        callid = call['CallId']
        listPK = call['ListPK']
        listSK = call['ListSK']
        print("CallID: ", callid, listPK, listSK)
        delete_meeting(callid, listPK, listSK)

    return { 'Result': "Meetings deleted successfully" }

def lambda_handler(event, context):
    print("Received event: " + json.dumps(event))
    if not verify_permissions(event):
        return { 'Result': "You don't have permission to share or delete one or more of the requested calls" }

    calls = event["arguments"]["input"]["Calls"]
    action = event["arguments"]["input"]["Action"]

    if(action == "Share"):
        recipients = event["arguments"]["input"]["MeetingRecipients"]
        return share_meetings(calls, recipients)
    elif(action == "Delete"):
        return delete_meetings(calls)
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
