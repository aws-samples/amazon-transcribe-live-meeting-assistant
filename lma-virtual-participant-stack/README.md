# Virtual Participant

This stack deploys an ECS Fargate Task and Step Function state machine architecture that will join meeting via 'Virtual Participant', or VP for short. The VP is launched via a Fargate Task that runs via [Playwright](https://playwright.dev/python/), a headless Chrome browser. The audio and meeting metadata are ingested and sent to the LMA Kinesis Data Stream for further processing. 

## How to test

The easiest way to test this is to execute the step function state machine. The payload is as follows:

```
{
  "apiInfo": {
    "httpMethod": "POST"
  },
  "data": {
    "meetingPlatform": "Zoom",
    "meetingID": "12345678",
    "meetingPassword": "a1b2c3",
    "meetingName": "A meeting title",
    "meetingTime": ""
  }
}
```

The `httpMethod` supports POST, GET, and DELETE.  If POST, if you provide a meeting time in the future, it will schedule a meeting in the future. If GET, it will fetch a list of scheduled meetings, and DELETE will delete the upcoming meeting. 

To execute this in the CLI, assuming you have AWS credentials, use this:

```
aws stepfunctions start-execution \
    --state-machine-arn arn:aws:states:us-east-1:123456789012:stateMachine:SchedulerStateMachine-09X1KR2ZL54I \
    --input '{"apiInfo": {"httpMethod": "POST"}, "data": {"meetingPlatform": "Zoom", "meetingID": "12345678", "meetingPassword": "a1b2c3", "meetingName": "A meeting title", "meetingTime": "", "userName": "Bob"}}'
```

## How to test VP via Docker

Use the following commands to test it locally in the browser. Note it is required to pass not only the meeting id and passcode, but also the LMA settings such as the Transcribe settings, Kinesis Data Stream name, etc.

`docker build -t scribe-transcribe . `
*Copy image id*
`docker run --env MEETING_ID=[Meeting ID here] --env MEETING_PASSWORD=[Meeting Password Here]  --env MEETING_NAME=testMeetingName --env AWS_DEFAULT_REGION=us-east-1 --env KINESIS_STREAM_NAME=[LMA's Kinesis Stream Here] --env CONTENT_REDACTION_TYPE=PII --env RECORDINGS_BUCKET_NAME=[bucket name here] --env RECORDINGS_KEY_PREFIX=lca-audio-recordings/ scribe-transcribe <imageid>`