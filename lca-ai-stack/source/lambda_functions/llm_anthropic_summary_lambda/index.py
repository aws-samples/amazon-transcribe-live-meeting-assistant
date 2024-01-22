# Invokes Anthropic generate text API using requests module
# see https://console.anthropic.com/docs/api/reference for more details

import sys
import os
import json
import re
import boto3
import requests

import logging
logger = logging.getLogger()
logger.setLevel(logging.ERROR)

# grab environment variables
ANTHROPIC_MODEL_IDENTIFIER = os.environ["ANTHROPIC_MODEL_IDENTIFIER"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ENDPOINT_URL = os.environ["ENDPOINT_URL"]
FETCH_TRANSCRIPT_LAMBDA_ARN = os.environ['FETCH_TRANSCRIPT_LAMBDA_ARN']
PROCESS_TRANSCRIPT = (os.getenv('PROCESS_TRANSCRIPT', 'False') == 'True')
TOKEN_COUNT = int(os.getenv('TOKEN_COUNT', '0')) # default 0 - do not truncate.
SUMMARY_PROMPT_SSM_PARAMETER = os.environ["SUMMARY_PROMPT_SSM_PARAMETER"]

lambda_client = boto3.client('lambda')
ssmClient = boto3.client("ssm")

def get_templates_from_ssm(prompt_override):
    templates = []
    prompt_template_str = None
    if prompt_override is not None:
        prompt_template_str = prompt_override
    if prompt_template_str is None:
        prompt_template_str = ssmClient.get_parameter(Name=SUMMARY_PROMPT_SSM_PARAMETER)["Parameter"]["Value"]

    try:
        prompt_templates = json.loads(prompt_template_str)
        for k, v in prompt_templates.items():
            prompt = v.replace("<br>", "\n")
            templates.append({ k:prompt })
    except:
        prompt = prompt_template_str.replace("<br>", "\n")
        templates.append({
            "Summary": prompt
        })
    return templates

def get_transcripts(callId):
    payload = {
        'CallId': callId, 
        'ProcessTranscript': PROCESS_TRANSCRIPT, 
        'TokenCount': TOKEN_COUNT 
    }
    print("Invoking lambda", payload)
    response = lambda_client.invoke(
        FunctionName=FETCH_TRANSCRIPT_LAMBDA_ARN,
        InvocationType='RequestResponse',
        Payload=json.dumps(payload)
    )
    print("Lambda response:", response)
    return response


def generate_anthropic_summary(transcript, prompt_override):
    # first check to see if this is one prompt, or many prompts as a json
    templates = get_templates_from_ssm(prompt_override)
    result = {}
    for item in templates:
        key = list(item.keys())[0]
        prompt = item[key]
        prompt = prompt.replace("{transcript}", transcript)
        data = {
            "prompt": prompt,
            "model": ANTHROPIC_MODEL_IDENTIFIER,
            "max_tokens_to_sample": 512,
            "stop_sequences": ["Human:", "Assistant:"]
        }
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "content-type": "application/json"
        }
        response = requests.post(ENDPOINT_URL, headers=headers, data=json.dumps(data))
        print("API Response:", response)
        summary = json.loads(response.text)["completion"].strip()
        result[key] = summary
    if len(result.keys()) == 1:
        # there's only one summary in here, so let's return just that.
        # this may contain json or a string.
        return result[list(result.keys())[0]]
    return json.dumps(result)


def handler(event, context):
    print("Received event: ", json.dumps(event))
    callId = event['CallId']
    transcript_response = get_transcripts(callId)
    transcript_data = transcript_response['Payload'].read().decode()
    print("Transcript data:", transcript_data)
    transcript_json = json.loads(transcript_data)
    transcript = transcript_json['transcript']
    
    summaryText = "No summary available"

    prompt_override = None
    if 'Prompt' in event:
        prompt_override = event['Prompt']

    try:
        summaryText = generate_anthropic_summary(transcript, prompt_override)
        print("Summary: ", summaryText)
        return {"summary": summaryText}
    except requests.exceptions.HTTPError as err:
        logger.error(err)
        raise

# for testing on terminal
if __name__ == "__main__":
    event = {
        "CallId": "8cfc6ec4-0dbe-4959-b1f3-34f13359826b"
    }
    handler(event)
