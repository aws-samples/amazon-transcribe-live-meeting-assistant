#!/bin/bash

echo "=== LMA Virtual Participant Startup ==="

# MicroVM dispatch.
#
# Under VPLaunchType=MICROVM the container must not run the VP app directly:
# the app needs per-meeting config, which does not exist yet at image-build
# time (it arrives later in the /run lifecycle hook). So hand off to the
# supervisor, which boots the pre-snapshot stack, serves the lifecycle hooks,
# and spawns the app once /run delivers the config.
#
# STACK_ONLY guards against recursion: the supervisor re-invokes THIS script
# with STACK_ONLY=true to bring the stack up, and that invocation must fall
# through to the boot sequence below rather than spawning another supervisor.
if [ "$VP_LAUNCH_TYPE" = "MICROVM" ] && [ "$STACK_ONLY" != "true" ]; then
    echo "=== MICROVM launch type: handing off to the lifecycle-hook supervisor ==="
    exec node /srv/dist/microvm-supervisor.js
fi

# Identify exactly which container image this task is running (build date +
# git commit), so logs make it obvious whether the expected code is deployed.
if [ -f /srv/build-info.json ]; then
    echo "=== VP build: $(cat /srv/build-info.json) ==="
else
    echo "=== VP build: (no build-info.json) ==="
fi

# Push BOOTING status before Node starts so the UI shows progress during cold-start.
push_booting_status() {
    if [ -z "$VIRTUAL_PARTICIPANT_ID" ] || [ -z "$VP_TABLE_NAME" ]; then
        return 0
    fi
    aws dynamodb update-item \
        --table-name "$VP_TABLE_NAME" \
        --key "{\"id\":{\"S\":\"$VIRTUAL_PARTICIPANT_ID\"}}" \
        --update-expression "SET #s = :s, updatedAt = :u" \
        --expression-attribute-names '{"#s":"status"}' \
        --expression-attribute-values "{\":s\":{\"S\":\"BOOTING\"},\":u\":{\"S\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\"}}" \
        --region "${AWS_REGION:-us-west-2}" \
        > /dev/null 2>&1 || true
}
push_booting_status &

echo "Starting D-Bus..."
dbus-daemon --system --fork 2>/dev/null || echo "D-Bus already running or not needed"

echo "Starting virtual display (Xvfb)..."
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset > /dev/null 2>&1 &
export DISPLAY=:99

echo "Waiting for display to initialize..."
sleep 3

# Suppress fluxbox toolbar so Chromium fills the full Xvfb screen.
mkdir -p ~/.fluxbox
cat > ~/.fluxbox/init <<'FLUXINIT'
session.screen0.toolbar.visible:        false
session.screen0.slit.autoHide:          true
session.screen0.fullMaximization:       true
session.screen0.workspaces:             1
session.screen0.tabs.usePixmap:         false
session.screen0.iconbar.usePixmap:      false
session.screen0.toolbar.autoHide:       true
session.screen0.toolbar.maxOver:        true
session.screen0.workspaceNames:         one,
session.screen0.titlebar.left:
session.screen0.titlebar.right:         Close
FLUXINIT

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
websockify \
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
# rate/channels are pinned on every sink: a null sink otherwise adopts the
# daemon default (48 kHz stereo), and each 16 kHz mono stream would then be
# resampled on the way in AND on the way out (GitHub #538).
MEETING_SINK=$(pactl load-module module-null-sink sink_name=meeting_audio rate=16000 channels=1 format=s16le sink_properties=device.description="Meeting_Audio")
echo "Created meeting_audio sink (module $MEETING_SINK)"

# Create a null sink for agent audio output (Nova/ElevenLabs)
AGENT_SINK=$(pactl load-module module-null-sink sink_name=agent_output rate=16000 channels=1 format=s16le sink_properties=device.description="Agent_Audio_Output")
echo "Created agent_output sink (module $AGENT_SINK)"

# Create a combined sink that mixes meeting + agent audio for transcription
COMBINED_SINK=$(pactl load-module module-null-sink sink_name=combined_audio rate=16000 channels=1 format=s16le sink_properties=device.description="Combined_Audio_For_Transcription")
echo "Created combined_audio sink (module $COMBINED_SINK)"

# 80ms latency: 1ms caused underruns on smaller instances, and 20ms still
# underran on a 2-vCPU MicroVM once Chromium, the avatar rescale, video recording
# and Transcribe were all competing (GitHub #543). These loopbacks feed
# transcription and the recording, so extra buffering costs nothing perceptible.
#
# This loopback is the SOLE route for meeting audio into combined_audio. The app
# used to ALSO pipe it in from scribe.ts, which delivered every utterance to
# Transcribe twice at two different latencies -- Transcribe then transcribed both
# ("Jack and Jill, Jack and Jill went up ...", GitHub #542). Do not re-add a
# second writer here or in the app.
pactl load-module module-loopback source=meeting_audio.monitor sink=combined_audio latency_msec=80
echo "Routed meeting audio to combined sink"

# Route agent_output.monitor to combined_audio sink
pactl load-module module-loopback source=agent_output.monitor sink=combined_audio latency_msec=80
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

# Assert the sinks really are 16 kHz mono. `pactl list short sinks` prints the
# sample spec, so a regression here (e.g. a sink created without rate=) shows up
# as a loud warning instead of only as degraded voice-assistant audio quality
# that takes a live meeting to notice (GitHub #538).
if pactl list short sinks | grep -qE 's16le[[:space:]]+1ch[[:space:]]+16000Hz'; then
    echo "✓ Audio sinks are 16 kHz mono — no resampling in the Nova/Transcribe path"
else
    echo "⚠️  WARNING: audio sinks are NOT 16 kHz mono. Nova audio will be resampled" >&2
    echo "    (warbly voice assistant). Expected 's16le 1ch 16000Hz'; got:" >&2
    pactl list short sinks >&2
fi
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

# MicroVM mode: everything above this line is the "pre-snapshot stack" (Xvfb,
# fluxbox, x11vnc, websockify, PulseAudio routing) and is exactly what we want
# captured in the Firecracker snapshot. The VP application itself must NOT start
# here, because per-meeting config does not exist yet — it arrives later in the
# /run lifecycle hook, and the supervisor spawns the app at that point.
#
# Reusing this script (rather than reimplementing the boot in TypeScript) keeps
# ECS and MicroVM on one code path, so audio-routing changes can't silently
# drift between them.
if [ "$STACK_ONLY" = "true" ]; then
    echo "=== STACK_ONLY: pre-snapshot stack is up; not starting the VP app ==="
    exit 0
fi

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
