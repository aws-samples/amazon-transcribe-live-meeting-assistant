#!/bin/sh

echo "Starting PulseAudio."
pulseaudio --start --daemon --exit-idle-time=-1 --log-target=syslog

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources

node dist/index.js