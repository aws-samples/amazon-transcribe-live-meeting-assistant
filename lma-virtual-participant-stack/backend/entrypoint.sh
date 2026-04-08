#!/bin/bash

echo "=== LMA Virtual Participant Startup ==="

echo "Starting D-Bus..."
dbus-daemon --system --fork 2>/dev/null || echo "D-Bus already running or not needed"

echo "Starting virtual display (Xvfb)..."
# Increased height to 1120 to account for window decorations and ensure full visibility
Xvfb :99 -screen 0 1920x1120x24 -ac +extension GLX +render -noreset > /dev/null 2>&1 &
export DISPLAY=:99

echo "Waiting for display to initialize..."
sleep 3

echo "Starting window manager (Fluxbox)..."
fluxbox > /dev/null 2>&1 &

echo "Starting VNC server..."
# Start x11vnc on standard VNC port 5900
x11vnc \
    -display :99 \
    -forever \
    -shared \
    -rfbport 5900 \
    -nopw \
    -xkb \
    -cursor arrow \
    -speeds lan \
    -wait 10 \
    -defer 10 \
    -noxdamage \
    > /tmp/x11vnc.log 2>&1 &

VNC_PID=$!
echo "VNC server started with PID: $VNC_PID on port 5900"

# Wait for VNC server to be ready
echo "Waiting for VNC server to be ready..."
sleep 2

# Check if VNC is listening on port 5900
MAX_ATTEMPTS=10
ATTEMPT=0
VNC_READY=false

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    if netstat -tuln | grep -q ":5900"; then
        echo "✓ VNC server is ready on port 5900"
        VNC_READY=true
        break
    fi
    echo "Waiting for VNC server... (attempt $((ATTEMPT + 1))/$MAX_ATTEMPTS)"
    sleep 1
    ATTEMPT=$((ATTEMPT + 1))
done

if [ "$VNC_READY" = false ]; then
    echo "ERROR: VNC server failed to start within timeout"
    echo "=== VNC Server Log ==="
    cat /tmp/x11vnc.log 2>/dev/null || echo "No log file found"
    echo "=== Process List ==="
    ps aux | grep -E "(x11vnc|websockify|Xvfb)" || echo "No VNC processes found"
    exit 1
fi

echo "Starting WebSocket proxy (websockify)..."
# Start websockify to proxy WebSocket connections from 5901 to VNC port 5900
# Bind to 0.0.0.0 to accept external connections (not just localhost)
/usr/share/novnc/utils/websockify/run \
    --web /usr/share/novnc \
    0.0.0.0:5901 \
    localhost:5900 \
    > /tmp/websockify.log 2>&1 &

WEBSOCKIFY_PID=$!
echo "Websockify started with PID: $WEBSOCKIFY_PID"

# Wait for websockify to be ready
echo "Waiting for websockify to be ready..."
sleep 2

# Check if websockify is listening on port 5901
ATTEMPT=0
WEBSOCKIFY_READY=false

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    if netstat -tuln | grep -q ":5901"; then
        echo "✓ WebSocket proxy is ready on port 5901"
        WEBSOCKIFY_READY=true
        break
    fi
    echo "Waiting for websockify... (attempt $((ATTEMPT + 1))/$MAX_ATTEMPTS)"
    sleep 1
    ATTEMPT=$((ATTEMPT + 1))
done

if [ "$WEBSOCKIFY_READY" = false ]; then
    echo "ERROR: Websockify failed to start within timeout"
    echo "=== Websockify Log ==="
    cat /tmp/websockify.log 2>/dev/null || echo "No log file found"
    echo "=== Process List ==="
    ps aux | grep -E "(websockify)" || echo "No websockify processes found"
    exit 1
fi

# Signal to Node.js app that VNC is ready
touch /tmp/vnc_ready
echo "✓ VNC ready signal created"

# Named pipe no longer needed - Chromium uses real PulseAudio device
echo "✓ Skipping named pipe creation (using real PulseAudio device)"

echo "Starting PulseAudio..."
pulseaudio --start --daemon --exit-idle-time=-1 --log-target=syslog

echo "Waiting for PulseAudio to be ready..."
sleep 2

echo "Creating PulseAudio audio routing for meeting and agent..."

# Create a null sink for meeting audio (Chromium output)
MEETING_SINK=$(pactl load-module module-null-sink sink_name=meeting_audio sink_properties=device.description="Meeting_Audio")
echo "Created meeting_audio sink (module $MEETING_SINK)"

# Create a null sink for agent audio output (Nova/ElevenLabs)
AGENT_SINK=$(pactl load-module module-null-sink sink_name=agent_output sink_properties=device.description="Agent_Audio_Output")
echo "Created agent_output sink (module $AGENT_SINK)"

# Create a combined sink that mixes meeting + agent audio for transcription
COMBINED_SINK=$(pactl load-module module-null-sink sink_name=combined_audio sink_properties=device.description="Combined_Audio_For_Transcription")
echo "Created combined_audio sink (module $COMBINED_SINK)"

# Route meeting_audio.monitor to combined_audio sink
# Note: latency_msec=1 was too aggressive and caused buffer underruns on smaller instances.
# 20ms provides a good balance between low latency and stability across instance sizes.
pactl load-module module-loopback source=meeting_audio.monitor sink=combined_audio latency_msec=20
echo "Routed meeting audio to combined sink"

# Route agent_output.monitor to combined_audio sink
pactl load-module module-loopback source=agent_output.monitor sink=combined_audio latency_msec=20
echo "Routed agent audio to combined sink"

# Create a virtual microphone source from agent_output for Chromium
pactl load-module module-remap-source source_name=agent_mic master=agent_output.monitor source_properties=device.description="Agent_Virtual_Microphone"
echo "Created agent_mic source for Chromium"

# Set meeting_audio as the default sink (Chromium will output here)
pactl set-default-sink meeting_audio
echo "Set meeting_audio as default sink"

echo "✓ Audio routing configured"

echo "PulseAudio Devices:"
echo "--- Sinks ---"
pactl list short sinks
echo "--- Sources ---"
pactl list short sources

echo ""
echo "🎤 Audio Routing Configuration:"
echo "   Chromium audio output → meeting_audio sink"
echo "   Nova audio output → agent_output sink"
echo "   Combined (meeting + agent) → combined_audio sink → Transcribe"
echo "   Meeting only → meeting_audio.monitor → Nova (no feedback!)"
echo "   Agent mic → agent_output.monitor → Chromium microphone"
echo ""
echo "✓ Barge-in enabled: Nova hears meeting audio only, not her own voice"

echo "=== Starting Virtual Participant Application ==="

# Check if running in dev mode
if [ "$DEV_MODE" = "true" ]; then
    echo "🔧 DEV MODE: Running with nodemon for auto-reload on file changes"
    echo "   Watching: src/**/*.ts"
    echo "   To manually rebuild: docker exec -it lma-vp-local-test npm run build"
    npm run dev:watch
else
    node dist/index.js
fi
