#!/bin/sh

echo "Starting PulseAudio."
pulseaudio --start

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources

python3 zoom.py
