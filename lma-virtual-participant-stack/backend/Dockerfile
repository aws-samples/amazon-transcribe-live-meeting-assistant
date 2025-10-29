
FROM public.ecr.aws/docker/library/node:20-alpine
USER root
WORKDIR /srv
COPY . /srv

RUN apk update && \
    apk add --no-cache \
    chromium \
    chromium-chromedriver \
    ffmpeg \
    pulseaudio \
    dbus-x11 \
    xvfb \
    x11vnc \
    fluxbox \
    font-liberation \
    alsa-lib \
    at-spi2-core \
    mesa-dri-gallium \
    libxkbcommon \
    libxcomposite \
    libxdamage \
    libxrandr \
    mesa-gbm \
    libxscrnsaver \
    nss \
    git \
    net-tools \
    curl

# Install noVNC and websockify for browser-based VNC access
RUN git clone https://github.com/novnc/noVNC.git /usr/share/novnc && \
    git clone https://github.com/novnc/websockify /usr/share/novnc/utils/websockify && \
    ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# Create Chromium managed policies for protocol handling
RUN mkdir -p /etc/chromium/policies/managed/ && \
    echo '{ "ExternalProtocolDialogShowAlwaysOpenCheckbox": false }' > /etc/chromium/policies/managed/dialog-policy.json && \
    echo '{ "URLAllowlist": ["https://*", "webex://*", "webexmeetings://*", "chime://*", "zoom://*", "zoommtg://*"] }' > /etc/chromium/policies/managed/allowlist.json && \
    echo '{ "AutoSelectCertificateForUrls": [], "DefaultBrowserSettingEnabled": false }' > /etc/chromium/policies/managed/browser-policy.json

# Environment variables for headless Chrome with X11
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
ENV DISPLAY=:99
ENV CHROME_BIN=/usr/bin/chromium

RUN npm ci && \
    npm run build

# Expose VNC port (5900) and WebSocket port (5901)
EXPOSE 5900 5901
    
RUN chmod +x /srv/entrypoint.sh
RUN adduser -D -h /home/appuser appuser
RUN chown -R appuser:appuser /srv
USER appuser
ENTRYPOINT ["/srv/entrypoint.sh"]
