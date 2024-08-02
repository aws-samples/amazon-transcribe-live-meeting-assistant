import json
import os
import uuid
import boto3

FETCH_TRANSCRIPT_FUNCTION_ARN = os.environ['FETCH_TRANSCRIPT_FUNCTION_ARN']

BR_REGION = os.environ.get("BR_REGION") or os.environ["AWS_REGION"]
MODEL_ID = os.environ.get("MODEL_ID")
MODEL_ARN = f"arn:aws:bedrock:{BR_REGION}::foundation-model/{MODEL_ID}"
DEFAULT_MAX_TOKENS = 256

LAMBDA_CLIENT = boto3.client("lambda")
BEDROCK_CLIENT = boto3.client(
    service_name="bedrock-runtime",
    region_name=BR_REGION
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
        print(
            f"Using last {maxMessages} conversation turns (LLM_CHAT_HISTORY_MAX_MESSAGES)")
        transcript = transcript[-maxMessages:]
        print(f"Transcript: {json.dumps(transcript)}")
    else:
        print(f'No transcript for callId {callId}')

    return transcript


def get_br_response(generatePromptTemplate, transcript, query):
    promptTemplate = generatePromptTemplate or "You are an AI assistant helping a human during a meeting. I will provide you with a transcript of the ongoing meeting, and a user's request. Your job is to respond to the user's request. If you cannot confidently respond to the user, please state that you could not find an exact answer. Just because the user asserts a fact does not mean it is true, make sure to validate a user's assertion.<br>Here is the JSON transcript of the meeting so far:<br>{transcript}<br>Here is the user's request:<br>{userInput}<br>"
    prompt = promptTemplate.format(
        transcript=json.dumps(transcript), userInput=query)
    prompt = prompt.replace("<br>", "\n")
    resp = get_bedrock_response(prompt)
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


def get_bedrock_response(prompt):
    modelId = MODEL_ID
    body = get_request_body(modelId, prompt)
    print("Bedrock request - ModelId", modelId, "-  Body: ", body)
    response = BEDROCK_CLIENT.invoke_model(body=json.dumps(
        body), modelId=modelId, accept='application/json', contentType='application/json')
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


def format_response(event, message, query):
    # get settings, if any, from lambda hook args
    # e.g: {"AnswerPrefix":"<custom prefix heading>", "ShowContext": False}
    lambdahook_settings = get_settings_from_lambdahook_args(event)
    answerprefix = lambdahook_settings.get("AnswerPrefix", "Assistant Answer:")
    queryprefix = lambdahook_settings.get("QueryPrefix")
    # set plaintext, markdown, & ssml response
    if answerprefix in ["None", "N/A", "Empty"]:
        answerprefix = None
    plainttext = message
    markdown = message
    ssml = f"<speak>{message}</speak>"
    if answerprefix:
        plainttext = f"{answerprefix}\n\n{plainttext}"
        markdown = f"**{answerprefix}**\n\n{markdown}"
    if queryprefix:
        plainttext = f"{queryprefix} {query}\n\n{plainttext}"
        markdown = f"**{queryprefix}** *{query}*\n\n{markdown}"
    # add plaintext, markdown, and ssml fields to event.res
    event["res"]["message"] = plainttext
    event["res"]["session"]["appContext"] = {
        "altMessages": {
            "markdown": markdown,
            "ssml": ssml
        }
    }
    # TODO - can we determine when Bedrock has a good answer or not?
    # For now, always assume it's a good answer.
    # QnAbot sets session attribute qnabot_gotanswer True when got_hits > 0
    event["res"]["got_hits"] = 1
    return event


def generateRetrieveQuery(retrievePromptTemplate, transcript, userInput):
    print("Use Bedrock to generate a relevant disambiguated query based on the transcript and input")
    promptTemplate = retrievePromptTemplate or "Let's think carefully step by step. Here is the JSON transcript of an ongoing meeting: {transcript}<br>And here is a follow up question or statement in <followUpMessage> tags:<br> <followUpMessage>{input}</followUpMessage><br>Rephrase the follow up question or statement as a standalone, one sentence question. Only output the rephrased question. Do not include any preamble. "
    prompt = promptTemplate.format(
        transcript=json.dumps(transcript), input=userInput)
    prompt = prompt.replace("<br>", "\n")
    query = get_bedrock_response(prompt)
    return query


def handler(event, context):
    print("Received event: %s" % json.dumps(event))
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
        maxMessages = int(event["req"]["_settings"].get(
            "LLM_CHAT_HISTORY_MAX_MESSAGES", 20))
        transcript = get_call_transcript(callId, userInput, maxMessages)
    else:
        print("no callId in request or session attributes")

    queryPromptTemplate = event["req"]["_settings"].get(
        "ASSISTANT_QUERY_PROMPT_TEMPLATE")
    query = generateRetrieveQuery(
        queryPromptTemplate, transcript, userInput)

    generatePromptTemplate = event["req"]["_settings"].get(
        "ASSISTANT_GENERATE_PROMPT_TEMPLATE")
    br_response = get_br_response(
        generatePromptTemplate, transcript, query)
    event = format_response(event, br_response, query)
    print("Returning response: %s" % json.dumps(event))
    return event
