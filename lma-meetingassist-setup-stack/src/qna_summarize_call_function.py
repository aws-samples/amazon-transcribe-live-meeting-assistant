import json
import boto3
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
    try:
        gmail_app = getenv("GMAIL_APP","")
        gmail_email = "kaip.meetingassist@gmail.com"
        recipients = ['mherrera@kaipartners.com','rshah@kaipartners.com']
        
        summary = ''
        summary_dict = json.loads(result["summary"])
        # Loop over the JSON key-value pairs
        for key, value in summary_dict.items():
            # Append the key and value to the summary string with line breaks for Markdown
            summary += f"## {key}\n\n{value}\n\n"
        
        # Replace escaped newline (\n) characters with actual newlines
        summary = summary.replace('\\n', '\n')

        html_content = markdown.markdown(summary)

        msg = MIMEMultipart("alternative")
        msg['Subject'] = 'Meeting Summary Information'
        msg['From'] = gmail_email
        msg['To'] = ', '.join(recipients)

        part1 = MIMEText(summary, "plain")
        part2 = MIMEText(html_content, "html")
        msg.attach(part1)
        msg.attach(part2)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp_server:
            smtp_server.login(gmail_email, gmail_app)
            smtp_server.sendmail(gmail_email, recipients, msg.as_string())
            print("Message sent!")
    except Exception as error:
        print(error)
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
