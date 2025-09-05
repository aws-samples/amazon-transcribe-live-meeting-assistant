#!/bin/sh
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#

echo "Starting PulseAudio."
pulseaudio --start

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources

python3 src/meeting.py
