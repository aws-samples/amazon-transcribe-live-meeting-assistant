# checkov:skip=CKV_DOCKER_2: "ECS target-group health check on 5901"

# Stock Chromium from Debian apt (the v0.3.3-proven browser). ECR Public
# mirror avoids Docker Hub anonymous rate limits in CodeBuild.
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    SCREEN_WIDTH=1920 \
    SCREEN_HEIGHT=1080 \
    SCREEN_DEPTH=24 \
    VNC_PORT=5900 \
    NOVNC_PORT=5901 \
    CHROME_BIN=/usr/bin/chromium \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

WORKDIR /srv

# Install stock Chromium from apt. Playwright launches this binary via
# executablePath (CHROME_BIN) — we do NOT use Playwright's bundled browser
# download. The Simli avatar's loopback WebRTC bridge works on stock
# Chromium (cloakbrowser's WebRTC patch broke it — 0 ICE candidates).
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
        libxcb1 libxext6 libxshmfence1 \
        libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
        libgdk-pixbuf-2.0-0 libxss1 libxtst6 fonts-liberation \
        libgl1-mesa-dri libegl-mesa0 \
        xvfb x11vnc fluxbox novnc websockify \
        fonts-noto-color-emoji fonts-freefont-ttf fonts-unifont \
        fonts-ipafont-gothic fonts-wqy-zenhei \
        pulseaudio pulseaudio-utils alsa-utils sox \
        dbus-x11 ffmpeg net-tools procps wget curl ca-certificates git tar gzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

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

RUN mkdir -p /srv/cloakbrowser-profiles

EXPOSE 5900 5901

RUN chmod +x /srv/entrypoint.sh
RUN useradd -m -d /home/appuser -s /bin/bash appuser
RUN chown -R appuser:appuser /srv
USER appuser
ENTRYPOINT ["/srv/entrypoint.sh"]
