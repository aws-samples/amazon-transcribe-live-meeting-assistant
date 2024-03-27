import json
import os
import uuid
import boto3

FETCH_TRANSCRIPT_FUNCTION_ARN = os.environ['FETCH_TRANSCRIPT_FUNCTION_ARN']

KB_ID = os.environ.get("KB_ID")
MODEL_ARN = os.environ.get("MODEL_ARN")
KB_REGION = os.environ.get("KB_REGION") or os.environ["AWS_REGION"]

LAMBDA_CLIENT = boto3.client("lambda")
KB_CLIENT = boto3.client(
    service_name="bedrock-agent-runtime",
    region_name=KB_REGION
)


def get_call_transcript(callId):
    payload = {
        'CallId': callId,
        'ProcessTranscript': True
    }
    lambda_response = LAMBDA_CLIENT.invoke(
        FunctionName=FETCH_TRANSCRIPT_FUNCTION_ARN,
        InvocationType='RequestResponse',
        Payload=json.dumps(payload)
    )
    result = json.loads(lambda_response.get("Payload").read().decode("utf-8"))
    transcriptSegments = result["transcript"].strip().split('\n')

    # Assign speaker name instead of role
    transcript = []
    for transcriptSegment in transcriptSegments:
        speaker, text = transcriptSegment.split(":", 1)
        transcript.append({"name": speaker, "transcript": text.strip()})

    print(f"Transcript: {json.dumps(transcript)}")
    return transcript


def get_kb_response(prompt):
    print(f"get_kb_response: prompt={prompt}, kb_id={KB_ID}")
    input = {
        "input": {
            'text': prompt
        },
        "retrieveAndGenerateConfiguration": {
            'knowledgeBaseConfiguration': {
                'knowledgeBaseId': KB_ID,
                'modelArn': MODEL_ARN
            },
            'type': 'KNOWLEDGE_BASE'
        }
    }
    print("Amazon Bedrock KB Request: ", input)
    try:
        resp = KB_CLIENT.retrieve_and_generate(**input)
    except Exception as e:
        print("Amazon Bedrock KB Exception: ", e)
        resp = {
            "systemMessage": "Amazon Bedrock KB Error: " + str(e)
        }
    print("Amazon Bedrock KB Response: ", json.dumps(resp))
    return resp


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


def format_response(event, kb_response):
    # get settings, if any, from lambda hook args
    # e.g: {"Prefix":"<custom prefix heading>", "ShowContext": False}
    lambdahook_settings = get_settings_from_lambdahook_args(event)
    prefix = lambdahook_settings.get("Prefix", "Amazon Bedrock KB Answer:")
    showContextText = lambdahook_settings.get("ShowContextText", True)
    showSourceLinks = lambdahook_settings.get("ShowSourceLinks", True)
    message = kb_response.get("output").get("text") or kb_response.get(
        "systemMessage") or "No answer found"
    # set plaintext, markdown, & ssml response
    if prefix in ["None", "N/A", "Empty"]:
        prefix = None
    plainttext = message
    markdown = message
    ssml = message
    if prefix:
        plainttext = f"{prefix}\n\n{plainttext}"
        markdown = f"**{prefix}**\n\n{markdown}"
    if showContextText:
        contextText = ""
        for source in kb_response.get("citations", []):
            for reference in source.get("retrievedReferences", []):
                snippet = reference.get("content", {}).get(
                    "text", "no reference text")
                url = reference.get("location", {}).get(
                    "s3Location", {}).get("uri")
                title = os.path.basename(url)
                if url:
                    contextText = f'{contextText}<br><a href="{url}">{title}</a>'
                else:
                    contextText = f'{contextText}<br><u><b>{title}</b></u>'
                contextText = f"{contextText}<br>{snippet}\n"
        if contextText:
            markdown = f'{markdown}\n<details><summary>Context</summary><p style="white-space: pre-line;">{contextText}</p></details>'
    if showSourceLinks:
        sourceLinks = []
        for citation in kb_response.get("citations", []):
            for retrievedReference in citation.get("retrievedReferences", []):
                # TODO - (1) convert s3 path to http. (2) support additional location types
                url = retrievedReference.get("location", {}).get(
                    "s3Location", {}).get("uri")
                title = os.path.basename(url)
                if url:
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
    # TODO - can we determine when Bedrock KB has a good answer or not?
    # For now, always assume it's a good answer.
    # QnAbot sets session attribute qnabot_gotanswer True when got_hits > 0
    event["res"]["got_hits"] = 1
    return event


def handler(event, context):
    print("Received event: %s" % json.dumps(event))
    args = get_args_from_lambdahook_args(event)
    # prompt set from args, or from user input if not specified in args.
    if event["req"].get("llm_generated_query"):
        userInput = event["req"]["llm_generated_query"]["orig"]
    else:
        userInput = event["req"]["question"]
    prompt = args.get("Prompt", userInput)
    # get transcript of current call and update prompt - callId set by agent orchestrator OR Lex Web UI
    callId = event["req"]["session"].get("callId") or event["req"]["_event"].get(
        "requestAttributes", {}).get("callId")
    if callId:
        transcript = get_call_transcript(callId)
        if transcript:
            # remove final segment if it matches the current input
            lastMessageText = transcript[-1]["transcript"]
            if lastMessageText == userInput:
                print("removing final segment as it matches the current input")
                transcript.pop()
        if transcript:
            maxMessages = int(event["req"]["_settings"].get(
                "LLM_CHAT_HISTORY_MAX_MESSAGES", 20))
            print(
                f"Using last {maxMessages} conversation turns (LLM_CHAT_HISTORY_MAX_MESSAGES)")
            transcript = transcript[-maxMessages:]
            prompt = f'You are an AI assistant helping a human during a meeting. Here is the meeting transcript: {json.dumps(transcript)}.'
            prompt = f'{prompt}\nPlease respond to the following request from the human, using the transcript and any additional information as context.\n{userInput}'
        else:
            print(f'No transcript for callId {callId}')
    else:
        print("no callId in request or session attributes")
    kb_response = get_kb_response(prompt)
    event = format_response(event, kb_response)
    print("Returning response: %s" % json.dumps(event))
    return event
