#!/bin/sh
#
# 
# Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
# or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
# 
#

echo "Starting PulseAudio."
pulseaudio --start

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources

python3 src/meeting.py
