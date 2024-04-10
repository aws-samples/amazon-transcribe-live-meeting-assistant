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

runtime = boto3.client('runtime.sagemaker')
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


def preprocess_transcripts(transcripts, condense, includeSpeaker):
    data = []

    transcripts.sort(key=lambda x: x['EndTime'])

    last_channel = 'start'
    for row in transcripts:
        transcript = row['Transcript']
        
        # prefix Speaker name to transcript segments if "IncludeSpeaker" parameter is set to True. 
        if includeSpeaker == True:
            # For LMA 'Hey Q' answers, we should keep assistant replies as part of the transcript for any contextual followup 'Hey Q' questions.
            if row['Channel'] == 'AGENT_ASSISTANT':
                # Add the 'MeetingAssistant:' prefix for assistant messages
                transcript = "MeetingAssistant: " + transcript
            else: 
                # Add the 'Speaker:' prefix for Transcript segments if "Speaker" field is present
                speakerName = row.get('Speaker', None)
                if speakerName:
                    transcript = speakerName.strip() + ': ' + transcript
                    
        if condense == True:
            transcript = remove_issues(transcript)
            transcript = remove_html(transcript)
            transcript = remove_filler_words(transcript).strip()

            if len(transcript) > 1:
                transcript = '\n' + transcript
        else:
            transcript = '\n' + transcript

        data.append(transcript)

    return data


def remove_issues(transcript_string):
    return re.sub(issue_remover, '', transcript_string)


def remove_html(transcript_string):
    return re.sub(html_remover, '', transcript_string)


def remove_filler_words(transcript_string):
    return re.sub(filler_remover, '', transcript_string)


def truncate_number_of_words(transcript_string, truncateLength):
    # findall can retain carriage returns
    data = re.findall(r'\S+|\n|.|,', transcript_string)
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

    includeSpeaker = False
    if 'IncludeSpeaker' in data:
        includeSpeaker = data['IncludeSpeaker']
        
    transcripts = get_transcripts(callid)
    transcripts = preprocess_transcripts(transcripts, preProcess, includeSpeaker)
    transcript_string = ''.join(transcripts)
    transcript_string = truncate_number_of_words(transcript_string, tokenCount)
    response = {'transcript': transcript_string}
    # print(transcript_string)
    return response


# Test case
if __name__ == '__main__':
    lambda_handler({
        "CallId": "2359fb61-f612-4fe9-bce2-839061c328f9",
        "TokenCount": 0,
        "ProcessTranscript": False,
        "LastNTurns": 20
    }, {})
