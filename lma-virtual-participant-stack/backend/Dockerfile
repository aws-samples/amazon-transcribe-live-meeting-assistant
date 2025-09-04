FROM public.ecr.aws/docker/library/python:3.12
USER root
WORKDIR /srv
COPY . /srv

ENV PIP_ROOT_USER_ACTION=ignore
RUN apt-get update && apt-get install -y \
    portaudio19-dev \
    pulseaudio \
    libnss3 \
    libnspr4 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libxss1 \
    libasound2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    libgdk-pixbuf-2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --upgrade pip
RUN pip3 install --default-timeout=20 -r requirements.txt
RUN playwright install chromium

RUN chmod +x /srv/entrypoint.sh
RUN mkdir -p /tmp/screenshots /tmp/recordings
RUN chmod 777 /tmp/screenshots /tmp/recordings
ENTRYPOINT ["/srv/entrypoint.sh"]
