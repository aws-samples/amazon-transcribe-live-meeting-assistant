#!/bin/sh

echo "Starting PulseAudio."
pulseaudio --start --daemon --exit-idle-time=-1 --log-target=syslog

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources
# npm run build
# export MEETING_ID=2486303316 
# export MEETING_PASSWORD= 
# export MEETING_NAME="Test chime bug" 
# export AWS_DEFAULT_REGION=us-east-1
# export AWS_REGION=us-east-1
# export KINESIS_STREAM_NAME=test-mig-CallDataStream-3roxfPmZb8Hd
# export CALL_DATA_STREAM_NAME=test-mig-CallDataStream-3roxfPmZb8Hd
# export CONTENT_REDACTION_TYPE=PII
# export SHOULD_RECORD_CALL=true
# export RECORDINGS_BUCKET_NAME=test-mig-recordingsbucket-iapnmyc9lxsg
# export RECORDINGS_KEY_PREFIX=lma-audio-recordings/ 
# export MEETING_PLATFORM=CHIME
# export USER_NAME=saidinta@amazon.com 
# export LMA_USER=saidinta@amazon.com 
# export INTRO_MESSAGE="Hello. I am an AI Live Meeting Assistant (LMA). I was invited by saidinta@amazon.com to join this call.  To learn more about me please visit: https://amazon.com/live-meeting-assistant." 
# export START_RECORDING_MESSAGE="Live Meeting Assistant started." 
# export STOP_RECORDING_MESSAGE="Live Meeting Assistant stopped." 
# export EXIT_MESSAGE="Live Meeting Assistant has left the room." 
# export LMA_IDENTITY="LMA (saidinta@amazon.com)" 
# export GRAPHQL_ENDPOINT="https://sygsna4pbbesbnhqj57nvepu7u.appsync-api.us-east-1.amazonaws.com/graphql" 
# export VP_TASK_REGISTRY_TABLE_NAME="your-vp-task-registry-table" 
# export DEBUG=DEBUG 

# node dist/index.js

# echo "Starting PulseAudio."
# pulseaudio --start --daemon --exit-idle-time=-1 --log-target=syslog

# echo "PulseAudio Info:"
# pactl list short sinks
# pactl list short sources

node dist/index.js