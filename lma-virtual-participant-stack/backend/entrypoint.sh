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
    -ncache 10 \
    -ncache_cr \
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
/usr/share/novnc/utils/websockify/run \
    --web /usr/share/novnc \
    5901 \
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

echo "Starting PulseAudio..."
pulseaudio --start --daemon --exit-idle-time=-1 --log-target=syslog

echo "PulseAudio Info:"
pactl list short sinks
pactl list short sources

echo "=== Starting Virtual Participant Application ==="
node dist/index.js
