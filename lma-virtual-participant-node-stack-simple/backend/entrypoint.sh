#!/bin/sh

echo "Starting PulseAudio."
pulseaudio --start --daemon --exit-idle-time=-1 --log-target=syslog

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources

MEETING_ID=2486303316 \
MEETING_PASSWORD= \
MEETING_NAME="Test chime bug" \
AWS_DEFAULT_REGION=us-east-1 \
AWS_REGION=us-east-1 \
KINESIS_STREAM_NAME=test-mig-CallDataStream-3roxfPmZb8Hd \
CALL_DATA_STREAM_NAME=test-mig-CallDataStream-3roxfPmZb8Hd \
CONTENT_REDACTION_TYPE=PII \
SHOULD_RECORD_CALL=true \
RECORDINGS_BUCKET_NAME=test-mig-recordingsbucket-iapnmyc9lxsg \
RECORDINGS_KEY_PREFIX=lma-audio-recordings/ \
MEETING_PLATFORM=Chime \
USER_NAME=saidinta@amazon.com \
LMA_USER=saidinta@amazon.com \
INTRO_MESSAGE="Hello. I am an AI Live Meeting Assistant (LMA). I was invited by jeremykf@amazon.com to join this call.  To learn more about me please visit: https://amazon.com/live-meeting-assistant." \
START_RECORDING_MESSAGE="Live Meeting Assistant started." \
STOP_RECORDING_MESSAGE="Live Meeting Assistant stopped." \
EXIT_MESSAGE="Live Meeting Assistant has left the room." \
LMA_IDENTITY="LMA (saidinta@amazon.com)" \
GRAPHQL_ENDPOINT="https://sygsna4pbbesbnhqj57nvepu7u.appsync-api.us-east-1.amazonaws.com/graphql" \
VP_TASK_REGISTRY_TABLE_NAME="your-vp-task-registry-table" \
DEBUG=DEBUG \

node dist/index.js
