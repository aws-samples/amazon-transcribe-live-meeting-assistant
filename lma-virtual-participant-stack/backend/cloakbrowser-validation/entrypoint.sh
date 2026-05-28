#!/usr/bin/env bash
# =============================================================================
# CloakBrowser validation entrypoint
# -----------------------------------------------------------------------------
# Brings up the same display stack the production virtual participant uses
# (Xvfb + fluxbox + x11vnc + websockify/noVNC) and then runs ONE of the two
# validation scripts (Playwright by default, Puppeteer on request).
#
# Override behavior:
#   MODE=playwright|puppeteer|python|manager   (default: playwright)
#   TARGET_URL=https://...                     (default: bot.sannysoft.com)
#   PROFILE_ID=<name>                          (default: random UUID per run)
#   FINGERPRINT_SEED=<10000-99999>             (default: random per run)
#   USE_FAKE_MEDIA=0|1                         (default: 1 — needed in containers)
#   WARMUP=0|1                                 (default: 1 — runs on fresh
#                                              profiles only; 3-phase warmup
#                                              with cloaktest stealth probes.
#                                              Cookie-exception prefs are
#                                              ALWAYS written regardless.)
#
# Ports:
#   5900 — raw VNC (connect with any VNC viewer: vnc://<host>:5900)
#   5901 — noVNC over WebSocket (browser: http://<host>:5901/vnc.html)
# =============================================================================
set -euo pipefail

MODE="${MODE:-playwright}"
SCREEN_WIDTH="${SCREEN_WIDTH:-1920}"
SCREEN_HEIGHT="${SCREEN_HEIGHT:-1080}"
SCREEN_DEPTH="${SCREEN_DEPTH:-24}"
DISPLAY_NUM="${DISPLAY:-:99}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-5901}"

echo "=== CloakBrowser Validation Startup ==="
echo "  MODE         = ${MODE}"
echo "  TARGET_URL   = ${TARGET_URL:-https://bot.sannysoft.com/}"
echo "  DISPLAY      = ${DISPLAY_NUM}"
echo "  VNC_PORT     = ${VNC_PORT}"
echo "  NOVNC_PORT   = ${NOVNC_PORT}"
echo "  AUTO_UPDATE  = ${AUTO_UPDATE:-0}"

# ---- Optional: pip + Chromium auto-update at container start ---------------
# Default OFF — see Dockerfile for rationale (latest 0.3.30 + Chromium .5
# regressed Zoom detection vs the pinned-known-good 0.3.26 + .3). Set
# AUTO_UPDATE=1 at `docker run` time to track upstream:
#
#     docker run -e AUTO_UPDATE=1 ...
#
# This re-runs `pip install -U cloakbrowser` and `python -m cloakbrowser
# update` every container start, which adds ~30s to startup but keeps you on
# the latest CloakBrowser release without rebuilding the image.
if [ "${AUTO_UPDATE:-0}" = "1" ] || [ "${AUTO_UPDATE:-0}" = "true" ]; then
    echo "[startup] AUTO_UPDATE=1 — upgrading cloakbrowser pip + Chromium binary…"
    # Drop the version-pinned download URL so `cloakbrowser update` picks the
    # latest binary from cloakbrowser.dev (otherwise we'd just re-fetch the
    # exact same pinned version we already have).
    unset CLOAKBROWSER_DOWNLOAD_URL
    # Allow the library's own background updater while we're at it.
    export CLOAKBROWSER_AUTO_UPDATE=true
    pip install --no-cache-dir --upgrade cloakbrowser \
        && python -m cloakbrowser update \
        && python -m cloakbrowser info
    echo "[startup] AUTO_UPDATE complete."
else
    echo "[startup] AUTO_UPDATE off (default). Set AUTO_UPDATE=1 to upgrade at start."
fi

# ---- Xvfb -------------------------------------------------------------------
echo "[startup] Starting Xvfb on ${DISPLAY_NUM} (${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH})…"
Xvfb "${DISPLAY_NUM}" -screen 0 "${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}" \
    -ac +extension GLX +render -noreset > /tmp/xvfb.log 2>&1 &
export DISPLAY="${DISPLAY_NUM}"
sleep 2

# ---- Fluxbox (window manager so --start-maximized has something to maximize against)
# Write a minimal init file BEFORE starting fluxbox so the toolbar
# (the gray strip at the bottom of the screen showing "Workspace 1 / clock /
# active window title") is suppressed. Without this fluxbox eats ~24px at the
# bottom of the screen and the Chromium window has to be sized smaller than
# the Xvfb display to fit, leaving an unsightly gray gap below it. Also turn
# off all the visual frills (slit, no menus on right-click, no edge resistance,
# no titlebar buttons except close) so the desktop looks as close to "real
# Windows desktop with just Chrome on it" as possible.
mkdir -p /root/.fluxbox
cat > /root/.fluxbox/init <<'FLUXINIT'
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
echo "[startup] Starting fluxbox window manager (toolbar hidden)…"
fluxbox > /tmp/fluxbox.log 2>&1 &
sleep 1

# ---- x11vnc (raw VNC on 5900) ----------------------------------------------
echo "[startup] Starting x11vnc on port ${VNC_PORT}…"
x11vnc \
    -display "${DISPLAY_NUM}" \
    -forever \
    -shared \
    -rfbport "${VNC_PORT}" \
    -nopw \
    -xkb \
    -cursor arrow \
    -noxdamage \
    > /tmp/x11vnc.log 2>&1 &

# Wait for x11vnc to actually bind the port before starting websockify
for _ in $(seq 1 10); do
    if netstat -tuln 2>/dev/null | grep -q ":${VNC_PORT} "; then
        echo "[startup] ✓ VNC ready on :${VNC_PORT}"
        break
    fi
    sleep 1
done

# ---- noVNC / websockify (browser-friendly VNC on 5901) ----------------------
echo "[startup] Starting websockify (noVNC) on port ${NOVNC_PORT}…"
# Debian's `novnc` package installs the web assets at /usr/share/novnc.
# websockify proxies WS<->TCP from 0.0.0.0:${NOVNC_PORT} -> localhost:${VNC_PORT}.
websockify --web=/usr/share/novnc "0.0.0.0:${NOVNC_PORT}" "localhost:${VNC_PORT}" \
    > /tmp/websockify.log 2>&1 &

for _ in $(seq 1 10); do
    if netstat -tuln 2>/dev/null | grep -q ":${NOVNC_PORT} "; then
        echo "[startup] ✓ noVNC ready on :${NOVNC_PORT} — open http://<host>:${NOVNC_PORT}/vnc.html"
        break
    fi
    sleep 1
done

# ---- Run the requested validation script ------------------------------------
case "${MODE}" in
    playwright)
        SCRIPT=/app/validate-playwright.mjs
        RUNNER=node
        ;;
    puppeteer)
        SCRIPT=/app/validate-puppeteer.mjs
        RUNNER=node
        ;;
    python)
        # Uses the Python `cloakbrowser` package (the EXACT same library the
        # CloakBrowser-Manager uses). Useful for isolating "JS wrapper has a
        # stealth bug" as the variable when both Playwright AND Puppeteer JS
        # variants fail Zoom but the Manager passes.
        SCRIPT=/app/validate-python.py
        RUNNER=python
        ;;
    manager)
        # Imports CloakBrowser-Manager's REAL BrowserManager class directly
        # from the cloned /opt/cloakbrowser-manager source tree. This is the
        # closest-to-Manager-image code path possible — same Python files,
        # same launch_persistent_context_async() invocation, same flag
        # construction, just without the Manager's FastAPI server / SQLite
        # DB / KasmVNC stack around it. If MODE=manager passes Zoom but
        # MODE=python doesn't, the variable is something the Manager does
        # in BrowserManager.launch() that we missed when porting.
        SCRIPT=/app/validate-manager.py
        RUNNER=python
        ;;
    *)
        echo "[startup] ERROR: unknown MODE='${MODE}'. Use 'playwright', 'puppeteer', 'python', or 'manager'." >&2
        exit 2
        ;;
esac

echo "[startup] Launching ${MODE} validation script: ${SCRIPT}"
cd /app
# `exec` so signals (SIGTERM from `docker stop`) reach the runner directly
# and the graceful shutdown handlers in the validation scripts can close
# the browser.
exec "${RUNNER}" "${SCRIPT}"
