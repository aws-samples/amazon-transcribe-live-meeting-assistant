# checkov:skip=CKV_DOCKER_2: "Health monitored by ECS target-group health check on port 5901 rather than container HEALTHCHECK"

# node:22-alpine has materially fewer CVEs than node:22-bookworm-slim (the
# Debian image flags ~30+ low/medium findings in ECR/Mirador scans on a
# fresh build, mostly in dynamically-linked glibc/openssl transitives that
# Alpine's musl/libressl variants avoid). Alpine ships chromium via apk so
# the meeting browser is built from the same source set as the OS, which
# also keeps the image ~300 MB smaller than the bookworm-slim + google-
# chrome-stable layout. Combined with rebrowser-puppeteer (CDP-level
# bot-detection patches at the runtime-binding layer), this base image
# moves us off the puppeteer-extra-plugin-stealth + Chrome Stable arms-
# race posture from the previous iteration.
FROM public.ecr.aws/docker/library/node:22-alpine3.21
USER root
WORKDIR /srv
COPY . /srv

RUN apk add --no-cache \
    bash \
    git \
    chromium \
    nss \
    freetype \
    harfbuzz \
    ca-certificates \
    ttf-freefont \
    py3-numpy \
    sox \
    dbus-x11 \
    xvfb \
    x11vnc \
    fluxbox \
    ffmpeg \
    pulseaudio \
    pulseaudio-utils \
    alsa-utils \
    net-tools \
    python3 \
    py3-pip

# Install noVNC and websockify for browser-based VNC access
RUN git clone --branch v1.7.0 --depth 1 https://github.com/novnc/noVNC.git /usr/share/novnc && \
    git clone --branch v0.13.0 --depth 1 https://github.com/novnc/websockify /usr/share/novnc/utils/websockify && \
    ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html

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

# Puppeteer should drive the system Chromium (apk-installed), not download
# its own bundled copy.
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
ENV DISPLAY=:99
ENV CHROME_BIN=/usr/bin/chromium

# Build the application + bundle simli-client into an ESM file the meeting
# page can import via fetch interception (avoids a CDN dependency).
RUN npm ci && \
    npm run build && \
    npx esbuild node_modules/simli-client/dist/index.js --bundle --format=esm --platform=browser --outfile=dist/simli-client.bundle.mjs

# Expose VNC port (5900) and WebSocket port (5901)
EXPOSE 5900 5901

RUN chmod +x /srv/entrypoint.sh
RUN adduser -D -h /home/appuser -s /bin/bash appuser
RUN chown -R appuser:appuser /srv
USER appuser
ENTRYPOINT ["/srv/entrypoint.sh"]
