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

lca_call_events = ddb.Table(LCA_CALL_EVENTS_TABLE)

def get_transcripts(callid):

    pk = 'trs#'+callid
    print(pk)

    try:
        response = lca_call_events.query(KeyConditionExpression=Key('PK').eq(pk), FilterExpression=(
            Attr('Channel').eq('AGENT') | Attr('Channel').eq('CALLER')) & Attr('IsPartial').eq(False))
        # response = lca_call_events.query(KeyConditionExpression=Key('PK').eq(pk))
    except ClientError as err:
        logger.error("Error getting transcripts from LCA Call Events table %s: %s",
                     err.response['Error']['Code'], err.response['Error']['Message'])
        raise
    else:
        # print(response['Items'])
        return response['Items']

def lambda_handler(event, context):
    print("Received event: " + json.dumps(event, indent=2))

    # Setup model input data using text (utterances) received from LCA
    data = json.loads(json.dumps(event))
    # callid = data['CallId']
    # response = {'transcript': transcript_string}
    # # print(transcript_string)
    # return response

    owner = get_owner_from_jwt(event['accessToken'], True)
    print("DECODED JWT", owner)
    return owner

# Test case
if __name__ == '__main__':
    lambda_handler({
        "CallId": "2359fb61-f612-4fe9-bce2-839061c328f9",
        "TokenCount": 0,
        "ProcessTranscript": False,
        "LastNTurns": 20
    }, {})
