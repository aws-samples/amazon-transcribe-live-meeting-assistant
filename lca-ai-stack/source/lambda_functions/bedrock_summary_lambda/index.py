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
BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
FETCH_TRANSCRIPT_LAMBDA_ARN = os.environ['FETCH_TRANSCRIPT_LAMBDA_ARN']
PROCESS_TRANSCRIPT = (os.getenv('PROCESS_TRANSCRIPT', 'False') == 'True')
TOKEN_COUNT = int(os.getenv('TOKEN_COUNT', '0')) # default 0 - do not truncate.
SUMMARY_PROMPT_SSM_PARAMETER = os.environ["SUMMARY_PROMPT_SSM_PARAMETER"]

# Optional environment variables allow region / endpoint override for bedrock Boto3
BEDROCK_REGION = os.environ["BEDROCK_REGION_OVERRIDE"] if "BEDROCK_REGION_OVERRIDE" in os.environ else os.environ["AWS_REGION"]
BEDROCK_ENDPOINT_URL = os.environ.get("BEDROCK_ENDPOINT_URL", f'https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com')

lambda_client = boto3.client('lambda')
ssmClient = boto3.client("ssm")
bedrock = boto3.client(service_name='bedrock-runtime', region_name=BEDROCK_REGION, endpoint_url=BEDROCK_ENDPOINT_URL) 

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

def get_request_body(modelId, prompt, max_tokens, temperature):
    provider = modelId.split(".")[0]
    request_body = None
    if provider == "anthropic":
        # claude-3 models use new messages format
        if modelId.startswith("anthropic.claude-3"):
            request_body = {
                "anthropic_version": "bedrock-2023-05-31",
                "messages": [{"role": "user", "content": [{'type': 'text', 'text': prompt}]}],
                "max_tokens": max_tokens,
                "temperature": temperature
            }
        else:
            request_body = {
                "prompt": prompt,
                "max_tokens_to_sample": max_tokens,
                "temperature": temperature
            }
    else:
        raise Exception("Unsupported provider: ", provider)
    return request_body

def get_generated_text(modelId, response):
    provider = modelId.split(".")[0]
    generated_text = None
    response_body = json.loads(response.get("body").read())
    print("Response body: ", json.dumps(response_body))
    if provider == "anthropic":
        # claude-3 models use new messages format
        if modelId.startswith("anthropic.claude-3"):
            generated_text = response_body.get("content")[0].get("text")
        else:
            generated_text = response_body.get("completion")
    else:
        raise Exception("Unsupported provider: ", provider)
    return generated_text

def call_bedrock(prompt_data):
    modelId = BEDROCK_MODEL_ID
    accept = 'application/json'
    contentType = 'application/json'
    body = get_request_body(modelId, prompt_data, max_tokens=512, temperature=0)
    print("Bedrock request - ModelId", modelId, "-  Body: ", body)
    response = bedrock.invoke_model(body=json.dumps(body), modelId=modelId, accept=accept, contentType=contentType)
    generated_text = get_generated_text(modelId, response)
    print("Bedrock response: ", json.dumps(generated_text))
    return generated_text

def generate_summary(transcript, prompt_override):
    # first check to see if this is one prompt, or many prompts as a json
    templates = get_templates_from_ssm(prompt_override)
    result = {}
    for item in templates:
        key = list(item.keys())[0]
        prompt = item[key]
        prompt = prompt.replace("{transcript}", transcript)
        response = call_bedrock(prompt)
        result[key] = response
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
    summary = "No summary available"

    prompt_override = None
    if 'Prompt' in event:
        prompt_override = event['Prompt']
    try:
        summary = generate_summary(transcript, prompt_override)
    except Exception as e:
        print(e)
        summary = 'An error occurred generating summary.'
    return {"summary": summary}
    
# for testing on terminal
if __name__ == "__main__":
    event = {
        "CallId": "8cfc6ec4-0dbe-4959-b1f3-34f13359826b"
    }
    handler(event)
