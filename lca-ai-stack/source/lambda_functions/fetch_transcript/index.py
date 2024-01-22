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

runtime= boto3.client('runtime.sagemaker')
logger = logging.getLogger(__name__)
ddb = boto3.resource('dynamodb')

issue_remover = re.compile('<span class=\'issue-pill\'>Issue Detected</span>')
html_remover = re.compile('<[^>]*>')
filler_remover = re.compile('(^| )([Uu]m|[Uu]h|[Ll]ike|[Mm]hm)[,]?')

lca_call_events = ddb.Table(LCA_CALL_EVENTS_TABLE)

def get_transcripts(callid):
    
    pk = 'trs#'+callid
    print(pk)
    
    try:
        response = lca_call_events.query(KeyConditionExpression=Key('PK').eq(pk), FilterExpression=(Attr('Channel').eq('AGENT') | Attr('Channel').eq('CALLER')) & Attr('IsPartial').eq(False))
        # response = lca_call_events.query(KeyConditionExpression=Key('PK').eq(pk)) 
    except ClientError as err:
        logger.error("Error getting transcripts from LCA Call Events table %s: %s", 
                err.response['Error']['Code'], err.response['Error']['Message'])
        raise 
    else:
        # print(response['Items'])
        return response['Items']

def preprocess_transcripts(transcripts, condense ):
    data = []

    transcripts.sort(key=lambda x: x['EndTime'])

    last_channel = 'start'
    for row in transcripts:
        transcript = row['Transcript']
        if condense == True:
          if row['Channel'] == 'AGENT_ASSISTANT':
              continue
          transcript = remove_issues(transcript)
          transcript = remove_html(transcript)
          transcript = remove_filler_words(transcript).strip()

          if row['Channel'] == last_channel:
              transcript = ' ' + transcript
          elif len(transcript) > 1:
              transcript = '\n' + row['Channel'] + ": " + transcript
              last_channel = row['Channel']
        else:
          transcript = '\n' + row['Channel'] + ": " + transcript
  
        data.append(transcript)
    
    return data

def remove_issues(transcript_string):
    return re.sub(issue_remover, '', transcript_string)

def remove_html(transcript_string):
    return re.sub(html_remover, '', transcript_string)

def remove_filler_words(transcript_string):
    return re.sub(filler_remover, '', transcript_string)

def truncate_number_of_words(transcript_string, truncateLength):
    #findall can retain carriage returns
    data = re.findall(r'\S+|\n|.|,',transcript_string)
    if truncateLength > 0:
      data = data[0:truncateLength]
    print('Token Count: ' + str(len(data)))
    return ''.join(data)


def lambda_handler(event, context):
    print("Received event: " + json.dumps(event, indent=2))
    
    # Setup model input data using text (utterances) received from LCA
    data = json.loads(json.dumps(event))
    callid = data['CallId']
    tokenCount = 0
    if 'TokenCount' in data:
        tokenCount = data['TokenCount']
    
    preProcess = False
    if 'ProcessTranscript' in data:
        preProcess = data['ProcessTranscript']

    transcripts = get_transcripts(callid)
    transcripts = preprocess_transcripts(transcripts, preProcess)
    transcript_string = ''.join(transcripts)
    transcript_string = truncate_number_of_words(transcript_string, tokenCount)
    response = { 'transcript': transcript_string }
    # print(transcript_string)
    return response

# Test case
if __name__ == '__main__':
    lambda_handler( {
        "CallId": "2359fb61-f612-4fe9-bce2-839061c328f9",
        "TokenCount": 0,
        "ProcessTranscript": False,
        "LastNTurns": 20
    }, {})