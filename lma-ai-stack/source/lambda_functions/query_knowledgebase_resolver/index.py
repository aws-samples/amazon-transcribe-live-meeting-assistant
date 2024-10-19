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

def get_kb_response(query, userId):
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

def handler(event, context):
    print("Received event: %s" % json.dumps(event))
    query = event["arguments"]["input"]
    userId = event["identity"]["username"]
    # future enhancements could allow additional metadata filters specified in the UI
    kb_response = json.dumps(get_kb_response(query, userId))
    print("Returning response: %s" % kb_response)
    return kb_response
