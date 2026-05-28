# checkov:skip=CKV_DOCKER_2: "Health monitored by ECS target-group health check on port 5901 rather than container HEALTHCHECK"

# CloakBrowser ships a glibc Chromium binary so we must use a glibc base
# (not Alpine/musl). python:3.12-slim is the same base the
# CloakBrowser-Manager reference image uses; cloakbrowser is a pip package
# that pre-downloads the matching Chromium binary at build time.
#
# The previous incarnation of this Dockerfile ran node:22-alpine + system
# `chromium` (apk) + rebrowser-puppeteer. Zoom kept bot-flagging fresh
# profiles from datacenter IPs even with rebrowser's CDP patches; the spike
# in `cloakbrowser-validation/` proved the CloakBrowser stealth Chromium
# (32 source-level C++ patches) passes Zoom where rebrowser does not.
# ECR Public mirror avoids Docker Hub anonymous rate limits in CodeBuild.
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    SCREEN_WIDTH=1920 \
    SCREEN_HEIGHT=1080 \
    SCREEN_DEPTH=24 \
    VNC_PORT=5900 \
    NOVNC_PORT=5901 \
    # Where cloakbrowser caches the stealth Chromium binary. `python -m
    # cloakbrowser install` writes here at build time; the JS package reads
    # the same env var at runtime.
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser \
    # Pin the Chromium version. We don't want a background updater silently
    # downloading a newer build that regresses Zoom detection (we hit this
    # in the validation work — 0.3.30 + .5 regressed vs 0.3.26 + .3).
    CLOAKBROWSER_AUTO_UPDATE=false

WORKDIR /srv

# -----------------------------------------------------------------------------
# 1. System packages: Chromium runtime libs + Xvfb/VNC + fonts + utilities.
# -----------------------------------------------------------------------------
# DO NOT install `chromium` from apt — we rely exclusively on the
# cloakbrowser-bundled binary. A system Chromium would be a footgun if
# anything later set executablePath by accident.
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Chromium runtime libraries
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
        libxcb1 libxext6 libxshmfence1 \
        libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
        libgdk-pixbuf-2.0-0 libxss1 libxtst6 fonts-liberation \
        libgl1-mesa-dri libegl-mesa0 \
        # Display + VNC stack
        xvfb x11vnc fluxbox novnc websockify \
        # Real fonts so canvas-fingerprint hashes match a real desktop
        fonts-noto-color-emoji fonts-freefont-ttf fonts-unifont \
        fonts-ipafont-gothic fonts-wqy-zenhei \
        # Audio routing for the VP (PulseAudio sinks for meeting / agent / mic)
        pulseaudio pulseaudio-utils alsa-utils sox \
        # Misc
        dbus-x11 ffmpeg net-tools procps wget curl ca-certificates git tar gzip \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (matches the previous Alpine image's Node version).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pinned: 0.3.29 chromium 146.0.7680.177.3 (same binary as 0.3.26).
# 0.3.29 is the first release exposing launchPersistentContext on the
# cloakbrowser/puppeteer subpath; we depend on it from src/index.ts.
ARG CLOAKBROWSER_VERSION=0.3.29

RUN pip install --no-cache-dir "cloakbrowser[geoip]==${CLOAKBROWSER_VERSION}" \
    && python -m cloakbrowser install \
    && python -m cloakbrowser info

# -----------------------------------------------------------------------------
# 3. Application source + dependencies.
# -----------------------------------------------------------------------------
COPY . /srv

# Chromium managed policy directory.
# - dialog-policy: don't show "Open external app?" dialogs.
# - allowlist: meeting-platform deeplink schemes.
# - browser-policy: don't try to be the default browser.
# - automation-policy: disable Chrome's "Save password?" / "Sign in to Chrome" /
#   Translate / etc. These browser-level UI overlays sit on top of the page DOM
#   and break our automation (e.g. Chrome's password-save bubble blocks the
#   Zoom skip button).
RUN mkdir -p /etc/chromium/policies/managed/ && \
    echo '{ "ExternalProtocolDialogShowAlwaysOpenCheckbox": false }' > /etc/chromium/policies/managed/dialog-policy.json && \
    echo '{ "URLAllowlist": ["https://*", "webex://*", "webexmeetings://*", "chime://*", "zoom://*", "zoommtg://*"] }' > /etc/chromium/policies/managed/allowlist.json && \
    echo '{ "AutoSelectCertificateForUrls": [], "DefaultBrowserSettingEnabled": false }' > /etc/chromium/policies/managed/browser-policy.json && \
    echo '{ "PasswordManagerEnabled": false, "AutofillCreditCardEnabled": false, "AutofillAddressEnabled": false, "BrowserSignin": 0, "SyncDisabled": true, "SafeBrowsingEnabled": false, "TranslateEnabled": false, "PromptForDownloadLocation": false, "DefaultNotificationsSetting": 2, "MetricsReportingEnabled": false }' > /etc/chromium/policies/managed/automation-policy.json

# Build the application + bundle simli-client into an ESM file the meeting
# page can import via fetch interception (avoids a CDN dependency).
RUN npm ci && \
    npm run build && \
    npx esbuild node_modules/simli-client/dist/index.js --bundle --format=esm --platform=browser --outfile=dist/simli-client.bundle.mjs

# Local on-disk profile root. On Fargate this is ephemeral (recreated per
# task); on EC2 with a persistent volume it can be reused across launches,
# saving the S3 download/extract round-trip.
RUN mkdir -p /srv/cloakbrowser-profiles

# Expose VNC port (5900) and WebSocket port (5901)
EXPOSE 5900 5901

RUN chmod +x /srv/entrypoint.sh
RUN useradd -m -d /home/appuser -s /bin/bash appuser
RUN chown -R appuser:appuser /srv /opt/cloakbrowser
USER appuser
ENTRYPOINT ["/srv/entrypoint.sh"]
