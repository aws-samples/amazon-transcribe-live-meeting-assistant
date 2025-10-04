
FROM public.ecr.aws/docker/library/node:20-slim
USER root
WORKDIR /srv
COPY . /srv

RUN apt-get update && \
    apt-get install -y \
    chromium \
    ffmpeg \
    pulseaudio

ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

RUN npm ci && \
    npm run build

RUN npx playwright install chromium

RUN groupadd -r appuser && useradd -r -g appuser appuser
    
RUN chmod +x /srv/entrypoint.sh
RUN chown -R appuser:appuser /srv
USER appuser
ENTRYPOINT ["/srv/entrypoint.sh"]
