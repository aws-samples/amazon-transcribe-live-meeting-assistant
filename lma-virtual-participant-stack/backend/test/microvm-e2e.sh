#!/usr/bin/env bash
#
# End-to-end test for the Virtual Participant MicroVM launch path, run against a
# real container image but WITHOUT AWS.
#
# It exercises the parts that unit tests cannot: that entrypoint.sh STACK_ONLY
# really brings up Xvfb/x11vnc/websockify/PulseAudio, that the supervisor serves
# the lifecycle hooks on the hook port, that /ready only returns 200 once the
# stack is actually serving (the snapshot correctness condition), and that /run
# injects per-meeting config into a spawned app process.
#
# Usage:
#   test/microvm-e2e.sh [image-tag]
#
# Requires: docker. Builds the image if the tag does not already exist.
# Set KEEP=1 to leave the container running for inspection.

set -uo pipefail

IMAGE="${1:-lma-vp-microvm-e2e}"
CONTAINER="lma-vp-microvm-e2e-run"
HOOK_PREFIX="/aws/lambda-microvms/runtime/v1"
HOOK_PORT=9000
PASS=0
FAIL=0

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
backend="$(dirname "$here")"

ok()   { echo "  PASS: $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $*"; FAIL=$((FAIL+1)); }
info() { echo "== $*"; }

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  else
    echo "KEEP=1: container '$CONTAINER' left running"
  fi
}
trap cleanup EXIT

info "Image: $IMAGE"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  info "Building image (this takes several minutes)..."
  docker build -t "$IMAGE" "$backend" || { echo "BUILD FAILED"; exit 1; }
fi

# Run the supervisor as the entrypoint, the way the MicroVM image config does.
# No AWS env is supplied: the point is to prove the launch mechanics work, and
# the app is expected to fail later for lack of AWS config (asserted below).
info "Starting container (real entrypoint; VP_LAUNCH_TYPE=MICROVM dispatches to the supervisor)"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -e VP_LAUNCH_TYPE=MICROVM \
  -e HOOK_PORT="$HOOK_PORT" \
  -e MICROVM_ENDPOINT=probe.lambda-microvm.us-west-2.on.aws \
  -e AWS_REGION=us-west-2 \
  -p 19000:9000 \
  "$IMAGE" >/dev/null || {
    echo "docker run failed"; exit 1; }

hook() { # $1 = hook name -> prints "HTTP_CODE BODY"
  docker exec "$CONTAINER" bash -lc \
    "curl -s -o /tmp/h.out -w '%{http_code}' -X POST 'http://127.0.0.1:${HOOK_PORT}${HOOK_PREFIX}/$1' \
       -H 'Content-Type: application/json' -d '${2:-}' ; echo -n ' ' ; cat /tmp/h.out" 2>/dev/null
}

info "Waiting for the hooks listener"
listener_up=0
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER" bash -lc "curl -sf http://127.0.0.1:${HOOK_PORT}/health >/dev/null 2>&1"; then
    listener_up=1; break
  fi
  sleep 2
done
[ "$listener_up" = "1" ] && ok "hooks listener is serving on :$HOOK_PORT" \
                        || { bad "hooks listener never came up"; docker logs "$CONTAINER" 2>&1 | tail -30; exit 1; }

# /ready boots the stack. It may legitimately return 503 while booting, then
# 200 — that ordering IS the contract, since 200 triggers the snapshot.
info "POST /ready (boots the pre-snapshot stack; 503 until healthy)"
saw_200=0
saw_503=0
for _ in $(seq 1 60); do
  resp="$(hook ready)"
  code="${resp%% *}"
  case "$code" in
    200) saw_200=1; break;;
    503) saw_503=1;;
  esac
  sleep 5
done
[ "$saw_200" = "1" ] && ok "/ready eventually returned 200 (stack healthy -> snapshot point)" \
                     || bad "/ready never returned 200"
# Whether a 503 is observed is a race (the stack may already be healthy by the
# first probe), so it is reported but not asserted. The deterministic version of
# this invariant — "503 until healthy, then 200" — is covered by the unit test
# '/ready returns 503 while the stack is not yet healthy'.
[ "$saw_503" = "1" ] && info "  (observed a 503 while booting: snapshot correctly deferred)" \
                     || info "  (stack was healthy on first probe; no 503 observed — not an error)"

info "Verifying the stack the snapshot would capture"
for chk in \
  "Xvfb:pgrep -x Xvfb" \
  "x11vnc:pgrep -x x11vnc" \
  "websockify:pgrep -f websockify" \
  "pulseaudio:pgrep -x pulseaudio" \
  ; do
  name="${chk%%:*}"; cmd="${chk#*:}"
  if docker exec "$CONTAINER" bash -lc "$cmd >/dev/null 2>&1"; then ok "$name running"; else bad "$name NOT running"; fi
done

# Query the display with xdotool, which the image already ships (xdpyinfo, from
# x11-utils, is deliberately not installed — no need to add a package just for
# this assertion). getdisplaygeometry fails if the X server is not answering.
if docker exec "$CONTAINER" bash -lc \
    'DISPLAY=:99 xdotool getdisplaygeometry 2>/dev/null | grep -qE "^1920 1080$"'; then
  ok "X display :99 answers with the expected 1920x1080 geometry"
else
  info "  geometry: $(docker exec "$CONTAINER" bash -lc 'DISPLAY=:99 xdotool getdisplaygeometry 2>&1' 2>/dev/null)"
  bad "X display :99 not answering"
fi

for p in 5900 5901; do
  if docker exec "$CONTAINER" bash -lc "curl -s --max-time 3 -o /dev/null http://127.0.0.1:$p || nc -z 127.0.0.1 $p" 2>/dev/null; then
    ok "port $p listening"
  else
    # netstat is present in the image; use it as the authoritative check.
    if docker exec "$CONTAINER" bash -lc "netstat -tln 2>/dev/null | grep -q ':$p '"; then
      ok "port $p listening"
    else
      bad "port $p not listening"
    fi
  fi
done

# The 3-sink barge-in topology is what makes agent audio not feed back into
# transcription; if it is missing from the snapshot, meetings break subtly.
if docker exec "$CONTAINER" bash -lc \
    'pactl list short sinks 2>/dev/null | awk "{print \$2}" | tr "\n" "," | grep -q "meeting_audio,agent_output,combined_audio"'; then
  ok "PulseAudio 3-sink topology present"
else
  info "  sinks: $(docker exec "$CONTAINER" bash -lc 'pactl list short sinks 2>/dev/null | awk "{print \$2}" | tr "\n" ","' 2>/dev/null)"
  bad "PulseAudio 3-sink topology missing"
fi

info "POST /validate"
resp="$(hook validate)"; code="${resp%% *}"
[ "$code" = "200" ] && ok "/validate returned 200" || bad "/validate returned $code"

# /validate is RETRIED by the platform, and it launches a real Chromium to warm
# the snapshot page cache. A fixed profile directory failed every retry with
# "Failed to create .../SingletonLock: File exists" because the first attempt's
# directory is captured in the snapshot. Assert a second call still warms.
info "POST /validate again (retry must still warm the workload)"
resp="$(hook validate)"; code="${resp%% *}"
[ "$code" = "200" ] && ok "/validate retry returned 200" || bad "/validate retry returned $code"
warmed=$(docker logs "$CONTAINER" 2>&1 | grep -c 'workloadWarmed=true' || true)
if [ "${warmed:-0}" -ge 2 ]; then
  ok "workload warmed on BOTH validate calls (snapshot prefetch will sample Chromium)"
else
  info "  warm count: ${warmed:-0}"
  info "  $(docker logs "$CONTAINER" 2>&1 | grep -i 'warm-up failed' | head -2)"
  bad "expected the workload to warm on each /validate call"
fi

info "POST /run with per-meeting config"
PAYLOAD='{"MEETING_PLATFORM":"Teams","MEETING_ID":"243574196567966","MEETING_NAME":"E2E probe","VIRTUAL_PARTICIPANT_ID":"vp-e2e","USER_ACCESS_TOKEN":"secret-token-value","MEETING_PASSWORD":"hunter2"}'
BODY="{\"microvmId\":\"mvm-e2e\",\"runHookPayload\":$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$PAYLOAD")}"
resp="$(hook run "$(echo "$BODY" | sed "s/'/'\\\\''/g")")"
code="${resp%% *}"
[ "$code" = "200" ] && ok "/run returned 200" || bad "/run returned $code"

sleep 12
logs="$(docker logs "$CONTAINER" 2>&1)"

# The app process is the proof that /run wired config through to a real spawn.
if echo "$logs" | grep -q "LMA Virtual Participant starting"; then
  ok "VP app was spawned by /run"
else
  bad "VP app did not start after /run"
fi

if echo "$logs" | grep -qE "Meeting Platform: Teams"; then
  ok "per-meeting config reached the app (MEETING_PLATFORM=Teams)"
else
  info "  $(echo "$logs" | grep -iE 'Meeting Platform|Meeting ID' | head -3)"
  bad "per-meeting config did not reach the app"
fi

# Regression guard: this is exactly what failed during the ARM64 probe, and the
# whole point of decoupling ALB registration from the MicroVM path.
if echo "$logs" | grep -q "Skipping ALB registration (MICROVM launch"; then
  ok "ALB self-registration correctly skipped under MICROVM"
else
  bad "expected the MICROVM ALB-skip message"
fi
if echo "$logs" | grep -q "ALB registration failed"; then
  bad "app hit the fatal ALB registration path under MICROVM"
else
  ok "no fatal ALB registration error"
fi

# Secrets must never reach logs (CloudWatch in production).
if echo "$logs" | grep -q "secret-token-value"; then
  bad "access token leaked into logs"
else
  ok "access token not present in logs"
fi
if echo "$logs" | grep -q "hunter2"; then
  bad "meeting password leaked into logs"
else
  ok "meeting password not present in logs"
fi

info "POST /terminate"
resp="$(hook terminate)"; code="${resp%% *}"
[ "$code" = "200" ] && ok "/terminate returned 200" || bad "/terminate returned $code"

echo
echo "==================== RESULT ===================="
echo "  passed: $PASS"
echo "  failed: $FAIL"
echo "================================================"
[ "$FAIL" -eq 0 ] || { echo "--- container logs (tail) ---"; docker logs "$CONTAINER" 2>&1 | tail -40; }
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
