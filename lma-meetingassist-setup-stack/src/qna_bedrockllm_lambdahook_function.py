import json
import os
import uuid
import boto3
from botocore.config import Config

FETCH_TRANSCRIPT_FUNCTION_ARN = os.environ['FETCH_TRANSCRIPT_FUNCTION_ARN']

BR_REGION = os.environ.get("BR_REGION") or os.environ["AWS_REGION"]
# use inference profile for model id as Nova models require the use of inference profiles
MODEL_ID = os.environ.get('MODEL_ID')
DEFAULT_MAX_TOKENS = 256

LAMBDA_CLIENT = boto3.client("lambda")
BEDROCK_CLIENT = boto3.client(
    service_name="bedrock-runtime",
    region_name=BR_REGION,
    config=Config(retries={'max_attempts': 50, 'mode': 'adaptive'})
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


def get_br_response(transcript, query, settings):

    promptTemplate = settings.get(
        "ASSISTANT_GENERATE_PROMPT_TEMPLATE")

    prompt = promptTemplate.format(
        transcript=json.dumps(transcript), userInput=query)
    prompt = prompt.replace("<br>", "\n")
    resp = get_bedrock_response(prompt, settings)
    return resp


def get_generate_text(response):
    return response["output"]["message"]["content"][0]["text"]


def get_bedrock_response(prompt, settings):
    modelId = MODEL_ID
    print("Bedrock request - ModelId", modelId)
    message = {
        "role": "user",
        "content": [{"text": prompt}]
    }
    response = BEDROCK_CLIENT.converse(
        modelId=modelId,
        messages=[message],
        inferenceConfig={
            "maxTokens": DEFAULT_MAX_TOKENS
        }
    )
    generated_text = get_generate_text(response)
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


def generateRetrieveQuery(retrievePromptTemplate, transcript, userInput, settings):
    print("Use Bedrock to generate a relevant disambiguated query based on the transcript and input")
    promptTemplate = retrievePromptTemplate or "Let's think carefully step by step. Here is the JSON transcript of an ongoing meeting: {transcript}<br>And here is a follow up question or statement in <followUpMessage> tags:<br> <followUpMessage>{input}</followUpMessage><br>Rephrase the follow up question or statement as a standalone, one sentence question. Only output the rephrased question. Do not include any preamble. "
    prompt = promptTemplate.format(
        transcript=json.dumps(transcript), input=userInput)
    prompt = prompt.replace("<br>", "\n")
    query = get_bedrock_response(prompt, settings)
    return query


def handler(event, context):
    print("Received event: %s" % json.dumps(event))
    args = get_args_from_lambdahook_args(event)
    settings = event["req"]["_settings"]
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
            "LLM_CHAT_HISTORY_MAX_MESSAGES", 20))
        transcript = get_call_transcript(callId, userInput, maxMessages)
    else:
        print("no callId in request or session attributes")

    queryPromptTemplate = settings.get(
        "ASSISTANT_QUERY_PROMPT_TEMPLATE")
    query = generateRetrieveQuery(
        queryPromptTemplate, transcript, userInput, settings)

    br_response = get_br_response(transcript, query, settings)
    event = format_response(event, br_response, query)
    print("Returning response: %s" % json.dumps(event))
    return event
