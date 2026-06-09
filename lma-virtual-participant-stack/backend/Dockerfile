# checkov:skip=CKV_DOCKER_2: "ECS target-group health check on 5901"

# CloakBrowser ships a glibc-only patched Chromium binary (pinned 0.3.29);
# ECR Public mirror avoids Docker Hub anonymous rate limits in CodeBuild.
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    SCREEN_WIDTH=1920 \
    SCREEN_HEIGHT=1080 \
    SCREEN_DEPTH=24 \
    VNC_PORT=5900 \
    NOVNC_PORT=5901 \
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser \
    CLOAKBROWSER_AUTO_UPDATE=false

WORKDIR /srv

# Do NOT install `chromium` from apt — we rely on the cloakbrowser binary
# (newer patched Chromium that handles Zoom's video encoder reliably on
# 2-vCPU hosts where stock Debian Chromium threw "Something went wrong").
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
        libxcb1 libxext6 libxshmfence1 \
        libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
        libgdk-pixbuf-2.0-0 libxss1 libxtst6 fonts-liberation \
        libgl1-mesa-dri libegl-mesa0 \
        xvfb x11vnc fluxbox novnc websockify xdotool \
        fonts-noto-color-emoji fonts-freefont-ttf fonts-unifont \
        fonts-ipafont-gothic fonts-wqy-zenhei \
        pulseaudio pulseaudio-utils alsa-utils sox \
        dbus-x11 ffmpeg net-tools procps wget curl ca-certificates git tar gzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pinned to match the npm cloakbrowser dep (see package.json). 0.3.31 brings the
# 58-patch fingerprint binary (Chrome 146 profiles) and a humanize iframe
# pointer-events fix; deps/peerDeps/engines are unchanged from 0.3.29 (no
# breaking changes verified against the changelog).
ARG CLOAKBROWSER_VERSION=0.3.31
RUN pip install --no-cache-dir "cloakbrowser[geoip]==${CLOAKBROWSER_VERSION}" \
    && python -m cloakbrowser install \
    && python -m cloakbrowser info

COPY . /srv

# Chrome managed policies: suppress UI overlays that block automation clicks.
RUN mkdir -p /etc/chromium/policies/managed/ && \
    echo '{ "ExternalProtocolDialogShowAlwaysOpenCheckbox": false }' > /etc/chromium/policies/managed/dialog-policy.json && \
    echo '{ "URLAllowlist": ["https://*", "webex://*", "webexmeetings://*", "chime://*", "zoom://*", "zoommtg://*"] }' > /etc/chromium/policies/managed/allowlist.json && \
    echo '{ "AutoSelectCertificateForUrls": [], "DefaultBrowserSettingEnabled": false }' > /etc/chromium/policies/managed/browser-policy.json && \
    echo '{ "PasswordManagerEnabled": false, "AutofillCreditCardEnabled": false, "AutofillAddressEnabled": false, "BrowserSignin": 0, "SyncDisabled": true, "SafeBrowsingEnabled": false, "TranslateEnabled": false, "PromptForDownloadLocation": false, "DefaultNotificationsSetting": 2, "MetricsReportingEnabled": false }' > /etc/chromium/policies/managed/automation-policy.json

RUN npm ci && \
    npm run build && \
    npx esbuild node_modules/simli-client/dist/index.js --bundle --format=esm --platform=browser --outfile=dist/simli-client.bundle.mjs

# Build stamp — lets logs/UI identify exactly which image a task is running.
# BUILD_DATE defaults to the in-build `date` (the true image build time);
# GIT_COMMIT / BUILD_SOURCE are passed by the builder (dev-push-vp.sh or
# CodeBuild) when available. Read at startup by entrypoint.sh + index.ts.
ARG BUILD_DATE=""
ARG GIT_COMMIT="unknown"
ARG BUILD_SOURCE="unknown"
RUN BD="${BUILD_DATE:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" && \
    printf '{"buildDate":"%s","gitCommit":"%s","buildSource":"%s"}\n' \
      "$BD" "$GIT_COMMIT" "$BUILD_SOURCE" > /srv/build-info.json && \
    echo "Build stamp: $(cat /srv/build-info.json)"

RUN mkdir -p /srv/cloakbrowser-profiles

EXPOSE 5900 5901

RUN chmod +x /srv/entrypoint.sh
RUN useradd -m -d /home/appuser -s /bin/bash appuser
RUN chown -R appuser:appuser /srv /opt/cloakbrowser
USER appuser
ENTRYPOINT ["/srv/entrypoint.sh"]
