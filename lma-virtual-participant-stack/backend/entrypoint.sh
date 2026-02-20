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

echo "Creating PulseAudio virtual microphone for agent audio..."
# Create a null sink for agent audio output
pactl load-module module-null-sink sink_name=agent_output sink_properties=device.description="Agent_Audio_Output"

# Create a virtual microphone source from the null sink's monitor
pactl load-module module-remap-source source_name=agent_mic master=agent_output.monitor source_properties=device.description="Agent_Virtual_Microphone"

echo "✓ Virtual microphone 'agent_mic' created"

echo "PulseAudio Devices:"
echo "--- Sinks ---"
pactl list short sinks
echo "--- Sources ---"
pactl list short sources

echo ""
echo "🎤 Virtual microphone available as: agent_mic"
echo "   Chromium reads from /tmp/mic_pipe"
echo "   Agent audio is played to 'agent_output' sink"
echo "   agent_mic monitors agent_output and streams to pipe"

echo "✓ Audio devices ready (Chromium will use agent_mic as microphone)"
echo ""
echo "🎧 Audio Routing:"
echo "   Meeting audio → 'default' source → FFmpeg → Transcribe + ElevenLabs"
echo "   Agent audio → agent_output sink → agent_mic source → Chromium → Meeting"
echo "   Feedback prevention: Agent audio blocked when isSpeaking=true"

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
