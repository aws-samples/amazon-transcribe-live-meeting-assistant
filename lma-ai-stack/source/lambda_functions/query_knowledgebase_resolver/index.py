# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
import json
import logging
import os

import boto3
from botocore.config import Config

# pylint: disable=import-error
from lma_config import (  # configuration layer
    LANGUAGE_MODELS,
    read_setting,
    resolve_model_id,
)
from lma_config import (
    model_arn as bedrock_model_arn,
)

# pylint: enable=import-error

# Questions, answers and the transcript passages they cite are logged at DEBUG
# only; set LOG_LEVEL to DEBUG to see them.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

KB_REGION = os.environ.get("KB_REGION") or os.environ["AWS_REGION"]
KB_ID = os.environ.get("KB_ID")
KB_ACCOUNT_ID = os.environ.get("KB_ACCOUNT_ID")


def model_arn():
    """The ARN of the model the knowledge base answers with.

    The model is the summaries' one: the Amazon Bedrock Models section's value
    on the Configuration page, else the shipped default in setting_defaults.json.
    Read per request, so a change reaches this function within the section's
    cache tier rather than at its next cold start. Run within the section's
    Model inference geography; a plain model id is addressed as a foundation
    model and any other as an inference profile.
    """
    model_id = resolve_model_id(
        read_setting(LANGUAGE_MODELS.name, "summaryModelId"), region=KB_REGION
    )
    return bedrock_model_arn(model_id, KB_REGION, KB_ACCOUNT_ID)


KB_CLIENT = boto3.client(
    service_name="bedrock-agent-runtime",
    region_name=KB_REGION,
    config=Config(retries={"max_attempts": 50, "mode": "adaptive"}),
)


def get_kb_response(query, userId, isAdminUser, sessionId):
    input = {
        "input": {"text": query},
        "retrieveAndGenerateConfiguration": {
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": KB_ID,
                "modelArn": model_arn(),
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {
                        "filter": {"equals": {"key": "Owner", "value": userId}}
                    }
                },
            },
            "type": "KNOWLEDGE_BASE",
        },
    }
    if isAdminUser:
        LOGGER.info("Admin user, no retrieval filters")
        input["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"].pop(
            "retrievalConfiguration", None
        )
    if sessionId:
        input["sessionId"] = sessionId
    LOGGER.debug("Amazon Bedrock KB Request: %s", input)
    try:
        resp = KB_CLIENT.retrieve_and_generate(**input)
    except Exception as e:
        LOGGER.warning("Amazon Bedrock KB Exception: %s", e)
        resp = {"systemMessage": "Amazon Bedrock KB Error: " + str(e)}
    LOGGER.debug("Amazon Bedrock KB Response: %s", json.dumps(resp))
    return resp


def markdown_response(kb_response):
    showContextText = True
    message = (
        kb_response.get("output", {}).get("text", {})
        or kb_response.get("systemMessage")
        or "No answer found"
    )
    markdown = message
    if showContextText:
        contextText = ""
        sourceLinks = []
        for source in kb_response.get("citations", []):
            for reference in source.get("retrievedReferences", []):
                snippet = reference.get("content", {}).get("text", "no reference text")
                callId = reference.get("metadata", {}).get("CallId")
                url = f"{callId}"
                title = callId
                contextText = (
                    f'{contextText}<br><callid href="{url}">{title}</callid><br>{snippet}\n'
                )
                sourceLinks.append(f'<callid href="{url}">{title}</callid>')
        if contextText:
            markdown = f'{markdown}\n<details><summary>Context</summary><p style="white-space: pre-line;">{contextText}</p></details>'
        if len(sourceLinks):
            markdown = f"{markdown}<br>Sources: " + ", ".join(sourceLinks)
    return markdown


def handler(event, context):
    LOGGER.debug("Received event: %s", json.dumps(event))
    query = event["arguments"]["input"]
    sessionId = event["arguments"].get("sessionId") or None
    userId = event["identity"]["username"]
    isAdminUser = False
    groups = event["identity"].get("groups")
    if groups:
        isAdminUser = "Admin" in groups
    kb_response = get_kb_response(query, userId, isAdminUser, sessionId)
    kb_response["markdown"] = markdown_response(kb_response)
    LOGGER.debug("Returning response: %s", json.dumps(kb_response))
    return json.dumps(kb_response)
