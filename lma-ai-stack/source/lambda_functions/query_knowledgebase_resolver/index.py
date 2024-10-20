import json
import os
import boto3

print("Boto3 version: ", boto3.__version__)

KB_REGION = os.environ.get("KB_REGION") or os.environ["AWS_REGION"]
KB_ID = os.environ.get("KB_ID")
MODEL_ID = os.environ.get("MODEL_ID")
MODEL_ARN = f"arn:aws:bedrock:{KB_REGION}::foundation-model/{MODEL_ID}"

KB_CLIENT = boto3.client(
    service_name="bedrock-agent-runtime",
    region_name=KB_REGION
)

def get_kb_response(query, userId, isAdminUser, sessionId):
    input = {
        "input": {
            'text': query
        },
        "retrieveAndGenerateConfiguration": {
            'knowledgeBaseConfiguration': {
                'knowledgeBaseId': KB_ID,
                'modelArn': MODEL_ARN,
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {
                        "filter": {
                            "equals": {
                                "key": "Owner",
                                "value": userId
                            }
                        }
                    }
                }
            },
            'type': 'KNOWLEDGE_BASE'
        }
    }
    if isAdminUser:
        print("Admin user, no retrieval filters")
        input["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"].pop("retrievalConfiguration", None)
    if sessionId:
        input["sessionId"] = sessionId
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


def markdown_response(kb_response):
    showContextText = True
    message = kb_response.get("output", {}).get("text", {}) or kb_response.get(
        "systemMessage") or "No answer found"
    markdown = message
    if showContextText:
        contextText = ""
        sourceLinks = []
        for source in kb_response.get("citations", []):
            for reference in source.get("retrievedReferences", []):
                snippet = reference.get("content", {}).get(
                    "text", "no reference text")
                callId = reference.get("metadata",{}).get("CallId")
                url = f"{callId}"
                title = callId
                contextText = f'{contextText}<br><callid href="{url}">{title}</callid><br>{snippet}\n'
                sourceLinks.append(f'<callid href="{url}">{title}</callid>')
        if contextText:
            markdown = f'{markdown}\n<details><summary>Context</summary><p style="white-space: pre-line;">{contextText}</p></details>'
        if len(sourceLinks):
            markdown = f'{markdown}<br>Sources: ' + ", ".join(sourceLinks)
    return markdown


def handler(event, context):
    print("Received event: %s" % json.dumps(event))
    query = event["arguments"]["input"]
    sessionId = event["arguments"].get("sessionId") or None
    userId = event["identity"]["username"]
    isAdminUser = "Admin" in event["identity"]["groups"]
    kb_response = get_kb_response(query, userId, isAdminUser, sessionId)
    kb_response["markdown"] = markdown_response(kb_response)
    print("Returning response: %s" % json.dumps(kb_response))
    return json.dumps(kb_response)
