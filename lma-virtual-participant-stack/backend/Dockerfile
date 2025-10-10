
FROM public.ecr.aws/docker/library/node:20-slim
USER root
WORKDIR /srv
COPY . /srv

RUN apt-get update && \
    apt-get install -y \
    chromium \
    ffmpeg \
    pulseaudio

RUN mkdir -p /etc/chromium/policies/managed/ && \
    echo '{ "ExternalProtocolDialogShowAlwaysOpenCheckbox": false }' > /etc/chromium/policies/managed/dialog-policy.json && \
    echo '{ "URLAllowlist": ["https://*", "webex://*", "webexmeetings://*"] }' > /etc/chromium/policies/managed/allowlist.json && \
    echo '{ "AutoSelectCertificateForUrls": [], "DefaultBrowserSettingEnabled": false }' > /etc/chromium/policies/managed/browser-policy.json



ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

RUN npm ci && \
    npm run build
    
RUN chmod +x /srv/entrypoint.sh
RUN useradd -m appuser
RUN chown -R appuser:appuser /srv
USER appuser
ENTRYPOINT ["/srv/entrypoint.sh"]
