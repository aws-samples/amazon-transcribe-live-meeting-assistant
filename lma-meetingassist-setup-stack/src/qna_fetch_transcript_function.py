import json
import boto3
import os
FETCH_TRANSCRIPT_FUNCTION_ARN = os.environ['FETCH_TRANSCRIPT_FUNCTION_ARN']
LAMBDA_CLIENT = boto3.client("lambda")

def get_call_transcript(callId):
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
    return result["transcript"]

def format_response(event, transcript):
    maxMessages = int(event["req"]["_settings"].get("LLM_CHAT_HISTORY_MAX_MESSAGES", 20))
    print(f"Using last {maxMessages} conversation turns (LLM_CHAT_HISTORY_MAX_MESSAGES)")
    transcriptSegments = transcript.strip().split('\n')
    # remove final segment if it matches the current utterance
    lastMessageRole, lastMessageText = transcriptSegments[-1].split(":")
    if lastMessageText.strip() == event["req"].get("question").strip():
      transcriptSegments.pop()
    transcriptSegments = transcriptSegments[-maxMessages:]
    chatHistory = []
    role, text = None, None
    for transcriptSegment in transcriptSegments:
      role, text = transcriptSegment.split(":")
      if role == "CALLER":
        chatHistory.append({"Human": text.strip()})
      else:
        chatHistory.append({"AI": text.strip()})
    event.setdefault("req",{}).setdefault("_userInfo",{})["chatMessageHistory"] = json.dumps(chatHistory)
    return event

def handler(event, context):
    print("Received event: %s" % json.dumps(event))
    # get callId from Request attributes.. set by LCA agentassist orchestrator
    callId = event["req"]["_event"].get("requestAttributes",{}).get("callId")
    if callId:
      print(f"Replacing chat history with call transcript for callId {callId}.")
      transcript = get_call_transcript(callId)
      event = format_response(event, transcript)
      # set callId sessionAttribute for possible later use in QnABot / Handlebars, etc.
      event["req"]["session"]["callId"] = callId
      event["res"]["session"]["callId"] = callId
    else:
      print("No callId session attribute - nothing to do")
    print("Returning response: %s" % json.dumps(event))
    return event
