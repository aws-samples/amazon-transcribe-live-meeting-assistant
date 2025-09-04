#!/bin/sh

echo "Starting PulseAudio."
pulseaudio --start
# pulseaudio --start --system --disallow-exit --disallow-module-loading

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources

# MEETING_ID=73350726154 \
# MEETING_PASSWORD=qM9fwm \
# MEETING_NAME="Test zoom bug" \
# AWS_DEFAULT_REGION=us-east-1 \
# KINESIS_STREAM_NAME=LMA-dev-stack-CallDataStream-eyoowQEAZZs6 \
# CONTENT_REDACTION_TYPE=PII \
# SHOULD_RECORD_CALL=true \
# RECORDINGS_BUCKET_NAME=lma-dev-stack-recordingsbucket-2ztuawowjqf6 \
# RECORDINGS_KEY_PREFIX=lma-audio-recordings/ \
# MEETING_PLATFORM=Zoom \
# USER_NAME=jeremykf@amazon.com \
# INTRO_MESSAGE="Hello. I am an AI Live Meeting Assistant (LMA). I was invited by jeremykf@amazon.com to join this call.  To learn more about me please visit: https://amazon.com/live-meeting-assistant." \
# START_RECORDING_MESSAGE="Live Meeting Assistant started." \
# STOP_RECORDING_MESSAGE="Live Meeting Assistant stopped." \
# EXIT_MESSAGE="Live Meeting Assistant has left the room." \
# LMA_IDENTITY="LMA (jeremykf@amazon.com)" \
# DEBUG=DEBUG \
python3 src/meeting.py
