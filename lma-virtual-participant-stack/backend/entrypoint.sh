#!/bin/sh
#
# 
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. 
# SPDX-License-Identifier: MIT-0
# 
#

echo "Starting PulseAudio."
pulseaudio --start

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources

python3 src/meeting.py
