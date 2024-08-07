import json
import boto3
import os
TRANSCRIPT_SUMMARY_FUNCTION_ARN = os.environ.get(
    "TRANSCRIPT_SUMMARY_FUNCTION_ARN")
LAMBDA_CLIENT = boto3.client("lambda")


def get_call_summary(callId, prompt):
    event = {"CallId": callId}
    if prompt:
        event["Prompt"] = prompt
    lambda_response = LAMBDA_CLIENT.invoke(
        FunctionName=TRANSCRIPT_SUMMARY_FUNCTION_ARN,
        InvocationType='RequestResponse',
        Payload=json.dumps(event)
    )
    result = json.loads(lambda_response.get("Payload").read().decode("utf-8"))
    return result["summary"]


def format_response(event, summary):
    # set plaintext, & markdown
    plainttext = summary
    markdown = summary
    ssml = f"<speak>{summary}</speak>"
    # add plaintext, markdown, and ssml fields to event.res
    event["res"]["message"] = plainttext
    event["res"]["session"]["appContext"] = {
        "altMessages": {
            "markdown": markdown,
            "ssml": ssml
        }
    }
    return event


def get_prompt_from_lambdahook_args(event):
    prompt = None
    lambdahook_args_list = event["res"]["result"].get("args", [])
    print("LambdaHook args: ", lambdahook_args_list)
    if len(lambdahook_args_list):
        prompt = lambdahook_args_list[0]
    return prompt


def handler(event, context):
    print("Received event: %s" % json.dumps(event))
    callId = event["req"]["session"].get("callId", {})
    prompt = get_prompt_from_lambdahook_args(event)
    summary = get_call_summary(callId, prompt)
    event = format_response(event, summary)
    print("Returning response: %s" % json.dumps(event))
    return event
