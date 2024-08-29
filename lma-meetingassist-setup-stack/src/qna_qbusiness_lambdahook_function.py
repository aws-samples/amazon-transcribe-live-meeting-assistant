import re
import base64
import json
import os
import random
import string
import uuid
import boto3

print("Boto3 version: ", boto3.__version__)

FETCH_TRANSCRIPT_FUNCTION_ARN = os.environ['FETCH_TRANSCRIPT_FUNCTION_ARN']

AMAZONQ_APP_ID = os.environ.get("AMAZONQ_APP_ID")
AMAZONQ_REGION = os.environ.get("AMAZONQ_REGION") or os.environ["AWS_REGION"]
AMAZONQ_ENDPOINT_URL = os.environ.get(
    "AMAZONQ_ENDPOINT_URL") or f'https://qbusiness.{AMAZONQ_REGION}.api.aws'
print("AMAZONQ_ENDPOINT_URL:", AMAZONQ_ENDPOINT_URL)

MODEL_ID = os.environ.get("MODEL_ID")
MODEL_ARN = f"arn:aws:bedrock:{AMAZONQ_REGION}::foundation-model/{MODEL_ID}"
DEFAULT_MAX_TOKENS = 256

LAMBDA_CLIENT = boto3.client("lambda")
BEDROCK_CLIENT = boto3.client(
    service_name="bedrock-runtime",
    region_name=AMAZONQ_REGION
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


def s3_uri_to_presigned_url(s3_uri, expiration=3600):
    # Extract bucket name and object key from S3 URI
    bucket_name, object_key = s3_uri[5:].split('/', 1)
    s3_client = boto3.client('s3')
    return s3_client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': bucket_name,
            'Key': object_key
        },
        ExpiresIn=expiration
    )


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


def format_response(event, amazonq_response, query):
    # get settings, if any, from lambda hook args
    # e.g: {"AnswerPrefix":"<custom prefix heading>", "ShowContext": False}
    lambdahook_settings = get_settings_from_lambdahook_args(event)
    answerprefix = lambdahook_settings.get("AnswerPrefix", "Assistant Answer:")
    showContextText = lambdahook_settings.get("ShowContextText", True)
    showSourceLinks = lambdahook_settings.get("ShowSourceLinks", True)
    queryprefix = lambdahook_settings.get("QueryPrefix")
    message = amazonq_response.get("output", {}).get("text", {}) or amazonq_response.get(
        "systemMessage") or "No answer found"
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
    if showContextText:
        contextText = ""
        refCount = 0
        for source in amazonq_response.get("sourceAttributions", []):
            refCount += 1
            title = source.get("title", f'Ref{refCount}')
            snippet = source.get("snippet", "snippet missing")
            url = source.get("url")
            if url:
                contextText = f'{contextText}<br><a href="{url}">{title}</a>'
            else:
                contextText = f'{contextText}<br><u><b>{title}</b></u>'
            # Returning too large of a snippet can break QnABot by exceeding the event payload size limit
            contextText = f"{contextText}<br>{snippet}\n"[:5000]
        if contextText:
            markdown = f'{markdown}\n<details><summary>Context</summary><p style="white-space: pre-line;">{contextText}</p></details>'
    if showSourceLinks:
        sourceLinks = []
        refCount = 0
        for source in amazonq_response.get("sourceAttributions", []):
            refCount += 1
            title = source.get("title", f'Ref{refCount}')
            url = source.get("url")
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
    # preserve conversation context in session
    amazonq_context = {
        "conversationId": amazonq_response.get("conversationId"),
        "parentMessageId": amazonq_response.get("systemMessageId")
    }
    event["res"]["session"]["qnabotcontext"]["amazonq_context"] = amazonq_context
    # TODO - can we determine when Amazon Q has a good answer or not?
    # For now, always assume it's a good answer.
    # QnAbot sets session attribute qnabot_gotanswer True when got_hits > 0
    event["res"]["got_hits"] = 1
    return event


def generateRetrieveQuery(retrievePromptTemplate, transcript, userInput):
    print("Use Bedrock to generate a relevant search query based on the transcript and input")
    promptTemplate = retrievePromptTemplate or "Let's think carefully step by step. Here is the JSON transcript of an ongoing meeting: {transcript}<br>And here is a follow up question or statement in <followUpMessage> tags:<br> <followUpMessage>{input}</followUpMessage><br>Rephrase the follow up question or statement as a standalone, one sentence question. If the caller is just engaging in small talk or saying thanks, respond with \"small talk\". Only output the rephrased question. Do not include any preamble."
    prompt = promptTemplate.format(
        transcript=json.dumps(transcript), input=userInput)
    prompt = prompt.replace("<br>", "\n")
    query = get_bedrock_response(prompt)
    return query


def get_amazonq_response(prompt, context, qbusiness_client):
    print(
        f"get_amazonq_response: prompt={prompt}, app_id={AMAZONQ_APP_ID}, context={context}")
    input = {
        "applicationId": AMAZONQ_APP_ID,
        "userMessage": prompt
    }
    if context:
        if context["conversationId"]:
            input["conversationId"] = context["conversationId"]
        if context["parentMessageId"]:
            input["parentMessageId"] = context["parentMessageId"]
    else:
        input["clientToken"] = str(uuid.uuid4())

    print("Amazon Q Input: ", input)
    try:
        resp = qbusiness_client.chat_sync(**input)
    except Exception as e:
        print("Amazon Q Exception: ", e)
        resp = {
            "systemMessage": "Amazon Q Error: " + str(e)
        }
    print("Amazon Q Response: ", json.dumps(resp, default=str))
    return resp


def get_idc_iam_credentials(jwt):
    sso_oidc_client = boto3.client('sso-oidc')
    idc_sso_resp = sso_oidc_client.create_token_with_iam(
        clientId=os.environ.get("IDC_CLIENT_ID"),
        grantType="urn:ietf:params:oauth:grant-type:jwt-bearer",
        assertion=jwt,
    )

    print(idc_sso_resp)
    idc_sso_id_token_jwt = json.loads(base64.b64decode(
        idc_sso_resp['idToken'].split('.')[1] + '==').decode())

    sts_context = idc_sso_id_token_jwt["sts:identity_context"]
    sts_client = boto3.client('sts')
    session_name = "qbusiness-idc-" + "".join(
        random.choices(string.ascii_letters + string.digits, k=32)
    )
    assumed_role_object = sts_client.assume_role(
        RoleArn=os.environ.get("AMAZONQ_ROLE_ARN"),
        RoleSessionName=session_name,
        ProvidedContexts=[{
            "ProviderArn": "arn:aws:iam::aws:contextProvider/IdentityCenter",
            "ContextAssertion": sts_context
        }]
    )
    creds_object = assumed_role_object['Credentials']

    return creds_object


def handler(event, context):
    print("Received event: %s" % json.dumps(event))
    args = get_args_from_lambdahook_args(event)
    # Any prompt value defined in the lambdahook args is used as UserInput, e.g used by
    # 'easy button' QIDs like 'Ask Assistant' where user didn't type a question, and we
    # just want a suggested reponse based on the transcript so far..
    # Otherwise we take the userInput from the users question in the request.
    qnabotcontext = event["req"]["session"].get("qnabotcontext", {})
    amazonq_context = qnabotcontext.get("amazonq_context", {})

    # Get the IDC IAM credentials
    # Parse session JWT token to get the jti
    if token := event.get("req", {}).get("session", {}).get("idtokenjwt"):
        print('Found token from chat interface')
    else:
        token = event['req']['_event']['requestAttributes'].get('idtokenjwt')
        print('Found token from voice interface')

    decoded_token = json.loads(base64.b64decode(
        token.split('.')[1] + '==').decode())
    jti = decoded_token['jti']

    dynamo_resource = boto3.resource('dynamodb')
    dynamo_table = dynamo_resource.Table(
        os.environ.get('DYNAMODB_CACHE_TABLE_NAME'))

    kms_client = boto3.client('kms')
    kms_key_id = os.environ.get("KMS_KEY_ID")

    # Check if JTI exists in caching DB
    response = dynamo_table.get_item(Key={'jti': jti})

    if 'Item' in response:
        creds = json.loads((kms_client.decrypt(
            KeyId=kms_key_id,
            CiphertextBlob=response['Item']['Credentials'].value))['Plaintext'])
    else:
        creds = get_idc_iam_credentials(token)
        exp = creds['Expiration'].timestamp()
        creds.pop('Expiration')
        # Encrypt the credentials and store them in the caching DB
        encrypted_creds = \
            kms_client.encrypt(KeyId=kms_key_id,
                               Plaintext=bytes(json.dumps(creds).encode()))['CiphertextBlob']
        dynamo_table.put_item(
            Item={'jti': jti, 'ExpiresAt': int(exp), 'Credentials': encrypted_creds})

    # Assume the qbusiness role with the IDC IAM credentials to create the qbusiness client
    assumed_session = boto3.Session(
        aws_access_key_id=creds['AccessKeyId'],
        aws_secret_access_key=creds['SecretAccessKey'],
        aws_session_token=creds['SessionToken']
    )

    qbusiness_client = assumed_session.client("qbusiness")
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
        
    retrievePromptTemplate = event["req"]["_settings"].get(
        "ASSISTANT_QUERY_PROMPT_TEMPLATE")
    query = generateRetrieveQuery(
        retrievePromptTemplate, transcript, userInput)
    
    prompt = query
    if transcript:
        prompt = f'You are an AI assistant helping a human during a meeting. Here is the meeting transcript: {json.dumps(transcript)}.'
        prompt = f'{prompt}\nPlease respond to the following request from the human, using the transcript and any additional information as context.\n{query}'
        
    amazonq_response = get_amazonq_response(prompt, amazonq_context, qbusiness_client)
    event = format_response(event, amazonq_response, query)
    print("Returning response: %s" % json.dumps(event))
    return event
