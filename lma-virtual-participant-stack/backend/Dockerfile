FROM public.ecr.aws/docker/library/python:3.12
USER root
WORKDIR /srv
COPY . /srv

ENV PIP_ROOT_USER_ACTION=ignore

# Update system packages first and install Chromium runtime dependencies
RUN apt-get update && apt-get install -y \
    portaudio19-dev \
    pulseaudio \
    libnspr4 \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libxkbcommon0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip3 install --upgrade pip
RUN pip3 install --default-timeout=20 -r requirements.txt

# Install Playwright and Chromium (without --with-deps since we installed dependencies above)
RUN playwright install chromium

RUN chmod +x /srv/entrypoint.sh
ENTRYPOINT ["/srv/entrypoint.sh"]
