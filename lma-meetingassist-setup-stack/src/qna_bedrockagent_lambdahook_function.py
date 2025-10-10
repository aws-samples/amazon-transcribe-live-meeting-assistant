# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
import json
import os
import boto3
import re
import time

print("Boto3 version: ", boto3.__version__)

FETCH_TRANSCRIPT_FUNCTION_ARN = os.environ['FETCH_TRANSCRIPT_FUNCTION_ARN']

BEDROCK_REGION = os.environ.get("BEDROCK_REGION") or os.environ["AWS_REGION"]
AGENT_ID = os.environ.get("AGENT_ID")
AGENT_ALIAS_ID = os.environ.get("AGENT_ALIAS_ID")
MODEL_ID = os.environ.get("MODEL_ID")
MODEL_ARN = f"arn:aws:bedrock:{BEDROCK_REGION}::foundation-model/{MODEL_ID}"
DEFAULT_MAX_TOKENS = 256

LAMBDA_CLIENT = boto3.client("lambda")
AGENT_CLIENT = boto3.client(
    service_name="bedrock-agent-runtime",
    region_name=BEDROCK_REGION
)
BEDROCK_CLIENT = boto3.client(
    service_name="bedrock-runtime",
    region_name=BEDROCK_REGION
)


def get_call_transcript(callId, userInput, maxMessages):
    payload = {
        'CallId': callId,
        'ProcessTranscript': True,
        'IncludeSpeaker': True
    }
    lambda_response = LAMBDA_CLIENT.invoke(
        FunctionName=FETCH_TRANSCRIPT_FUNCTION_ARN,
        InvocationType='RequestResponse',
        Payload=json.dumps(payload)
    )
    result = json.loads(lambda_response.get("Payload").read().decode("utf-8"))
    transcriptSegments = result["transcript"].strip().split('\n')

    transcript = []
    for transcriptSegment in transcriptSegments:
        speaker, text = transcriptSegment.split(":", 1)
        transcript.append({"name": speaker, "transcript": text.strip()})

    if transcript:
        # remove final segment if it matches the current input
        lastMessageText = transcript[-1]["transcript"]
        if lastMessageText == userInput:
            print("removing final segment as it matches the current input")
            transcript.pop()

    if transcript:
        if maxMessages > 0:
            print(
                f"Using last {maxMessages} conversation turns (LLM_CHAT_HISTORY_MAX_MESSAGES)")
            transcript = transcript[-maxMessages:]
        print(f"Transcript: {json.dumps(transcript)}")
    else:
        print(f'No transcript for callId {callId}')
    print("Transcript Length: ", len(json.dumps(transcript)))
    return transcript


def get_agent_response(transcript, userInput, callId, settings):

    # create generate prompt
    promptTemplate = settings.get(
        "ASSISTANT_GENERATE_PROMPT_TEMPLATE", "{userInput}")
    inputText = promptTemplate.format(
        transcript=json.dumps(transcript), userInput=userInput)
    inputText = inputText.replace("<br>", "\n")
    # make a unique sessionId for each agent invocation.. we do not need agent to retain conversation
    # memory since we will pass the latest meeting transcript in promptSessionAttributes for
    # context, rather than the interaction history with the agent.
    sessionId = "UniqueSessionId:" + str(time.time())
    input = {
        "agentAliasId": AGENT_ALIAS_ID,
        "agentId": AGENT_ID,
        "inputText": inputText,
        "sessionId": sessionId,
        "sessionState": {
            "sessionAttributes": {
                "callId": callId
            },
            "promptSessionAttributes": {
                "Meeting Transcript:": json.dumps(transcript)
            }
        }
    }
    print("Amazon Bedrock Invoke Agent Request: ", input)
    try:
        response = AGENT_CLIENT.invoke_agent(**input)
        completion = ""
        citations = []
        c = 0
        for event in response.get("completion"):
            chunk = event["chunk"]
            print(f"Amazon Bedrock Agent Response Chunk ({c}): ", chunk)
            c = c + 1
            completion = completion + chunk["bytes"].decode()
            citations.extend(
                chunk.get("attribution", {}).get("citations", []))
        resp = {
            "completion": completion,
            "citations": citations
        }
    except Exception as e:
        print("Amazon Bedrock Agent Exception: ", e)
        resp = {
            "systemMessage": "Amazon Bedrock Agent Error: " + str(e)
        }
    print("Amazon Bedrock Agent Response: ", json.dumps(resp))
    return resp


def get_request_body(modelId, prompt):
    provider = modelId.split(".")[0]
    request_body = None
    if provider == "anthropic":
        # claude-3 models use new messages format
        if modelId.startswith("anthropic.claude-3"):
            request_body = {
                "anthropic_version": "bedrock-2023-05-31",
                "messages": [{"role": "user", "content": [{'type': 'text', 'text': prompt}]}],
                "max_tokens": DEFAULT_MAX_TOKENS
            }
        else:
            request_body = {
                "prompt": prompt,
                "max_tokens_to_sample": DEFAULT_MAX_TOKENS
            }
    else:
        raise Exception("Unsupported provider: ", provider)
    return request_body


def get_generate_text(modelId, response):
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


def get_bedrock_response(prompt, settings):
    modelId = MODEL_ID
    body = get_request_body(modelId, prompt)
    args = dict(
        body=json.dumps(body),
        modelId=modelId,
        accept='application/json',
        contentType='application/json'
    )
    # optional guardrails config
    guardrailIdentifier = settings.get(
        "ASSISTANT_BEDROCK_GUARDRAIL_ID", "")
    if guardrailIdentifier:
        args["guardrailIdentifier"] = guardrailIdentifier
        args["guardrailVersion"] = str(settings.get(
            "ASSISTANT_BEDROCK_GUARDRAIL_VERSION", "DRAFT"))
    print("Bedrock request args - ", args)
    response = BEDROCK_CLIENT.invoke_model(**args)
    generated_text = get_generate_text(modelId, response)
    print("Bedrock response: ", generated_text)
    return generated_text


def get_settings_from_lambdahook_args(event):
    lambdahook_settings = {}
    lambdahook_args_list = event["res"]["result"].get("args", [])
    print("LambdaHook args: ", lambdahook_args_list)
    if len(lambdahook_args_list):
        try:
            lambdahook_settings = json.loads(lambdahook_args_list[0])
        except Exception as e:
            print(f"Failed to parse JSON:", lambdahook_args_list[0], e)
            print("..continuing")
    return lambdahook_settings


def get_args_from_lambdahook_args(event):
    parameters = {}
    lambdahook_args_list = event["res"]["result"].get("args", [])
    print("LambdaHook args: ", lambdahook_args_list)
    if len(lambdahook_args_list):
        try:
            parameters = json.loads(lambdahook_args_list[0])
        except Exception as e:
            print(f"Failed to parse JSON:", lambdahook_args_list[0], e)
            print("..continuing")
    return parameters


def s3_uri_to_presigned_url(s3_uri, expiration=3600):
    # Extract bucket name and object key from S3 URI
    bucket_name, object_key = s3_uri[5:].split('/', 1)
    s3_client = boto3.client('s3')
    try:
        signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': bucket_name,
                'Key': object_key
            },
            ExpiresIn=expiration
        )
    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        print("..continuing with unsigned URL")
        signed_url = s3_uri
    return signed_url


def get_url_from_reference(reference):
    location_keys = {
        "S3": "s3Location",
        "WEB": "webLocation",
        "CONFLUENCE": "confluenceLocation",
        "SALESFORCE": "salesforceLocation",
        "SHAREPOINT": "sharepointLocation"
    }
    location = reference.get("location", {})
    type = location.get("type")
    if type == "S3":
        uri = location.get(
            location_keys.get(type, {}), {}).get("uri")
        url = s3_uri_to_presigned_url(uri)
    else:
        url = location.get(
            location_keys.get(type, {}), {}).get("url")
    if not url:
        # try getting url from the metadata tags instead
        url = reference.get("metadata", {}).get(
            "x-amz-bedrock-kb-source-uri")
    return url


def format_response(event, agent_response):
    # get settings, if any, from lambda hook args
    # e.g: {"AnswerPrefix":"<custom prefix heading>", "ShowContext": False}
    lambdahook_settings = get_settings_from_lambdahook_args(event)
    answerprefix = lambdahook_settings.get("AnswerPrefix", "Assistant Answer:")
    showContextText = lambdahook_settings.get("ShowContextText", True)
    showSourceLinks = lambdahook_settings.get("ShowSourceLinks", True)
    message = agent_response.get("completion") or "No answer found"
    # set plaintext, markdown, & ssml response
    if answerprefix in ["None", "N/A", "Empty"]:
        answerprefix = None
    plainttext = message
    markdown = message
    ssml = f"<speak>{message}</speak>"
    if answerprefix:
        plainttext = f"{answerprefix}\n\n{plainttext}"
        markdown = f"**{answerprefix}**\n\n{markdown}"
    if showContextText:
        contextText = ""
        refCount = 0
        for source in agent_response.get("citations", []):
            for reference in source.get("retrievedReferences", []):
                refCount += 1
                snippet = reference.get("content", {}).get(
                    "text", "no reference text")
                url = get_url_from_reference(reference)
                if url:
                    # get title from url - handle presigned urls by ignoring path after '?'
                    title = os.path.basename(
                        url.split('?')[0]) or f'Ref{refCount}'
                    contextText = f'{contextText}<br><a href="{url}">{title}</a>'
                else:
                    contextText = f"{contextText}<br>{snippet}\n"
                contextText = f"{contextText}<br>{snippet}\n"
        if contextText:
            markdown = f'{markdown}\n<details><summary>Context</summary><p style="white-space: pre-line;">{contextText}</p></details>'
    if showSourceLinks:
        sourceLinks = []
        refCount = 0
        for source in agent_response.get("citations", []):
            for reference in source.get("retrievedReferences", []):
                refCount += 1
                url = get_url_from_reference(reference)
                if url:
                    # get title from url - handle presigned urls by ignoring path after '?'
                    title = os.path.basename(
                        url.split('?')[0]) or f'Ref{refCount}'
                    sourceLinks.append(f'<a href="{url}">{title}</a>')
        if len(sourceLinks):
            markdown = f'{markdown}<br>Sources: ' + ", ".join(sourceLinks)

    # add plaintext, markdown, and ssml fields to event.res
    event["res"]["message"] = plainttext
    event["res"]["session"]["appContext"] = {
        "altMessages": {
            "markdown": markdown,
            "ssml": ssml
        }
    }
    # Check plaintext answer for match using ASSISTANT_NO_HITS_REGEX
    pattern = re.compile(event["req"]["_settings"].get(
        "ASSISTANT_NO_HITS_REGEX", "Sorry,"))
    match = re.search(pattern, plainttext)
    if match:
        print("No hits found in response.. setting got_hits to 0")
        event["res"]["got_hits"] = 0
    else:
        event["res"]["got_hits"] = 1
    return event


def handler(event, context):
    print("Received event: %s" % json.dumps(event))
    settings = event["req"]["_settings"]
    args = get_args_from_lambdahook_args(event)
    # Any prompt value defined in the lambdahook args is used as UserInput, e.g used by
    # 'easy button' QIDs like 'Ask Assistant' where user didn't type a question, and we
    # just want a suggested reponse based on the transcript so far..
    # Otherwise we take the userInput from the users question in the request.
    userInput = args.get("Prompt")
    if not userInput:
        if event["req"].get("llm_generated_query"):
            userInput = event["req"]["llm_generated_query"]["orig"]
        else:
            userInput = event["req"]["question"]

    # get transcript of current call - callId set by agent orchestrator OR Lex Web UI
    transcript = None
    callId = event["req"]["session"].get("callId") or event["req"]["_event"].get(
        "requestAttributes", {}).get("callId")
    if callId:
        maxMessages = int(settings.get(
            "LLM_CHAT_HISTORY_MAX_MESSAGES", 0))
        transcript = get_call_transcript(callId, userInput, maxMessages)
    else:
        print("no callId in request or session attributes")

    agent_response = get_agent_response(
        transcript, userInput, callId, settings)

    event = format_response(event, agent_response)
    print("Returning response: %s" % json.dumps(event))
    return event
