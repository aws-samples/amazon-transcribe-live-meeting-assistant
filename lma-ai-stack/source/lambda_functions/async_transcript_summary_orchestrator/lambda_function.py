#!/usr/bin/env python3.12
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from os import getenv
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Any
import json
import requests
import re
import markdown
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
# third-party imports from Lambda layer
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
import boto3
from botocore.config import Config as BotoCoreConfig
from eventprocessor_utils import (
    get_meeting_ttl
)
import jwt

# pylint: enable=import-error
LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_kinesis.client import KinesisClient
    from boto3 import Session as Boto3Session
else:
    Boto3Session = object
    LambdaClient = object
    KinesisClient = object

BOTO3_SESSION: Boto3Session = boto3.Session()
CLIENT_CONFIG = BotoCoreConfig(
    read_timeout=int(getenv("BOTO_READ_TIMEOUT", '60')),
    retries={"mode": "adaptive", "max_attempts": 3},
)

LAMBDA_CLIENT: LambdaClient = BOTO3_SESSION.client(
    "lambda",
    config=CLIENT_CONFIG,
)
KINESIS_CLIENT: KinesisClient = BOTO3_SESSION.client(
    "kinesis"
)

TRANSCRIPT_SUMMARY_FUNCTION_ARN = getenv("TRANSCRIPT_SUMMARY_FUNCTION_ARN", "")
CALL_DATA_STREAM_NAME = getenv("CALL_DATA_STREAM_NAME", "")

def get_user_email(access_token):
    cognito_userinfo_endpoint = "https://jtc-uat-1746644220046210180.auth.us-east-1.amazoncognito.com/oauth2/userInfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(cognito_userinfo_endpoint, headers=headers)
    if response.status_code == 200:
        user_info = response.json()
        email = user_info.get("email")  # Extract the email attribute
        return email
    else:
        print(response)
        return 'error, cognito api failed'

def get_call_summary(
    message: Dict[str, Any]
):
    print(message)
    decoded_jwt = jwt.decode(message["AccessToken"], options={"verify_signature": False})
    try:
        user_email = get_user_email(message["AccessToken"])
    except Exception as e:
        print(e)
        user_email = 'mherrera@kaipartners.com'
        
    print(decoded_jwt)
    print(user_email)
    meeting_title = message["CallId"]
    meeting_datetime = meeting_title.split('- ')[-1]
    meeting_datetime = datetime.strptime(meeting_datetime, '%Y-%m-%d-%H:%M:%S.%f').strftime('%B %d, %Y %I:%M %p')
    subject = f"{meeting_title} Summary Information"
    lambda_response = LAMBDA_CLIENT.invoke(
        FunctionName=TRANSCRIPT_SUMMARY_FUNCTION_ARN,
        InvocationType='RequestResponse',
        Payload=json.dumps(message)
    )
    try:
        message = json.loads(lambda_response.get(
            "Payload").read().decode("utf-8"))
    except Exception as error:
        LOGGER.error(
            "Transcript summary result payload parsing exception. Lambda must return JSON object with (modified) input event fields",
            extra=error,
        )
    try:
        email_pw = getenv("EMAIL_PW","")
        support_email = "support@kaipartners.com"
        recipients = []
        if user_email not in recipients:
            recipients.append(user_email)
        summary = f"## Meeting Title\n{meeting_title}\n\n## Date\n{meeting_datetime}\n\n"
        summary_dict = json.loads(message["summary"])
        # Loop over the JSON key-value pairs
        for key, value in summary_dict.items():
            # Append the key and value to the summary string with line breaks for Markdown
            summary += f"## {key}\n\n{value}\n\n"
        
        # Replace escaped newline (\n) characters with actual newlines
        summary = summary.replace('\\n', '\n')

        html_content = markdown.markdown(summary)

        msg = MIMEMultipart("alternative")
        msg['Subject'] = subject
        msg['From'] = support_email
        msg['To'] = ', '.join(recipients)

        part1 = MIMEText(summary, "plain")
        part2 = MIMEText(html_content, "html")
        msg.attach(part1)
        msg.attach(part2)
        with smtplib.SMTP("smtp.office365.com", 587) as server:
            server.starttls()  # Secure the connection
            server.login(support_email, email_pw)
            server.sendmail(support_email, recipients, msg.as_string())
            print("Message sent!")
    except Exception as error:
        print(error)
    return message


def write_call_summary_to_kds(
    message: Dict[str, Any]
):
    callId = message.get("CallId", None)
    expiresAfter = message.get("ExpiresAfter", get_meeting_ttl())

    new_message = dict(
        CallId=callId,
        EventType="ADD_SUMMARY",
        ExpiresAfter=expiresAfter,
        CallSummaryText=message["CallSummaryText"]
    )

    if callId:
        try:
            KINESIS_CLIENT.put_record(
                StreamName=CALL_DATA_STREAM_NAME,
                PartitionKey=callId,
                Data=json.dumps(new_message)
            )
            LOGGER.info("Write ADD_SUMMARY event to KDS")
        except Exception as error:
            LOGGER.error(
                "Error writing ADD_SUMMARY event to KDS ",
                extra=error,
            )
    return


@LOGGER.inject_lambda_context
def handler(event, context: LambdaContext):
    # pylint: disable=unused-argument
    """Lambda handler"""
    LOGGER.debug("Transcript summary lambda event", extra={"event": event})

    data = json.loads(json.dumps(event))

    call_summary = get_call_summary(message=data)

    LOGGER.debug("Call summary: ")
    LOGGER.debug(call_summary)
    data['CallSummaryText'] = call_summary['summary']

    write_call_summary_to_kds(data)