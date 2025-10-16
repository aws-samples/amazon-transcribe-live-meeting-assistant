#!/bin/bash

echo "=== LMA Virtual Participant Startup ==="

echo "Starting D-Bus..."
dbus-daemon --system --fork 2>/dev/null || echo "D-Bus already running or not needed"

echo "Starting virtual display (Xvfb)..."
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset > /dev/null 2>&1 &
export DISPLAY=:99

echo "Waiting for display to initialize..."
sleep 3

echo "Starting window manager (Fluxbox)..."
fluxbox > /dev/null 2>&1 &

echo "Starting VNC server with WebSocket support..."
# Run x11vnc with built-in WebSocket support on port 5901
# -httpport enables WebSocket connections (noVNC can connect directly)
x11vnc \
    -display :99 \
    -forever \
    -shared \
    -rfbport 5901 \
    -httpport 5901 \
    -http_oneport \
    -nopw \
    -xkb \
    -cursor arrow \
    -ncache 10 \
    -ncache_cr \
    -speeds lan \
    -wait 10 \
    -defer 10 \
    -noxdamage \
    > /tmp/x11vnc.log 2>&1 &

VNC_PID=$!
echo "VNC server started with PID: $VNC_PID"

# Wait for VNC server to be ready
echo "Waiting for VNC server to be ready..."
sleep 3

# Check if VNC WebSocket is actually listening
MAX_ATTEMPTS=10
ATTEMPT=0
VNC_READY=false

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    if netstat -tuln | grep -q ":5901"; then
        echo "✓ VNC WebSocket server is ready on port 5901"
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

# Signal to Node.js app that VNC is ready
touch /tmp/vnc_ready
echo "✓ VNC ready signal created"

echo "Starting PulseAudio..."
pulseaudio --start --daemon --exit-idle-time=-1 --log-target=syslog

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources

echo "=== Starting Virtual Participant Application ==="
node dist/index.js
