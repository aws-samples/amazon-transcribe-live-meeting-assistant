# checkov:skip=CKV_DOCKER_2: "ECS target-group health check on 5901"

# CloakBrowser ships a glibc-only patched Chromium binary (see the pin below);
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

# This pip package is BUILD-TIME ONLY: it downloads the patched Chromium into
# CLOAKBROWSER_CACHE_DIR. Nothing at runtime imports it — the VP drives the
# browser through the *npm* `cloakbrowser` client (src/index.ts), so this pin
# and package.json must stay on the same version line to keep the wrapper that
# lays the binary down in step with the client that launches it.
#
# It had drifted badly: 0.3.31 here vs npm 0.5.x in package.json. Realigned to
# 0.5.10. `cloakbrowser info` reports the *identical* binary on both
# (chromium-146.0.7680.177.5), so the patched browser — and therefore the
# fingerprint surface — is unchanged by this bump; only the downloader moved.
# The 0.5.x CLI keeps the `install`/`info` subcommands used below, and its new
# free-tier note ("more than one concurrent session" needs a key) is satisfied
# by design: one container per meeting runs exactly one browser session.
#
# The ARG is deliberately NOT named CLOAKBROWSER_VERSION. Docker exposes ARGs as
# environment variables to RUN, and cloakbrowser 0.5.x reads $CLOAKBROWSER_VERSION
# as a *Chromium binary* version pin — so the wrapper version leaked in as a
# browser pin and `install` died with "Invalid browser version pin. Use a full
# numeric Chromium version, e.g. '148.0.7778.215.2'." 0.3.31 ignored the
# variable, which is why the old pin never tripped over it. Verified by docker
# build; do not rename this back.
ARG CLOAKBROWSER_PIP_VERSION=0.5.10
RUN pip install --no-cache-dir "cloakbrowser[geoip]==${CLOAKBROWSER_PIP_VERSION}" \
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

# 5900 raw VNC, 5901 noVNC/websockify, 9000 Lambda MicroVMs lifecycle hooks.
# 9000 is unused (and unexposed to any listener) under the ECS launch types.
EXPOSE 5900 5901 9000

RUN chmod +x /srv/entrypoint.sh
RUN useradd -m -d /home/appuser -s /bin/bash appuser

# Pin PulseAudio to the pipeline's native format: 16 kHz mono.
#
# PulseAudio's compiled default is 48 kHz stereo, and every stage of the VP audio
# path is 16 kHz mono PCM16 (Nova Sonic in and out, Transcribe, the pacat
# playback streams). With the default, Nova's voice was resampled 16k->48k into
# agent_output, again through the loopback into combined_audio, and again through
# module-remap-source into Chromium's virtual mic — three conversions before the
# meeting heard it, which made the assistant sound warbly (GitHub #538).
#
# resample-method: PulseAudio defaults to speex-float-1, tuned for speed. Any
# residual conversion should favour quality; speex-float-5 is still cheap.
# Written as the appuser's own config since the daemon runs as that user.
#
# NOTE: `avoid-resampling = yes` is deliberately NOT set. It makes PulseAudio
# RECONFIGURE a device to a client's rate instead of resampling for it, which
# does nothing here — every sink already carries an explicit
# `rate=16000 channels=1 format=s16le` (entrypoint.sh), and that pinning is what
# actually removed the triple resample. What it can do is harm: Teams opens the
# agent_mic remap source TWICE with conflicting formats (one mono capture with
# echoCancellation/AGC on, one stereo capture with them off — both observed live
# via the audio diagnostics), so two clients demand incompatible formats from one
# monitor source. Zoom opens it once, and Zoom is the platform that sounds fine.
# Device re-negotiation glitches audio without dropping an RTP packet, which
# matches every measurement taken for #543: the container-side recording is
# clean, packet counters are clean, and only the meeting listener hears garble.
RUN mkdir -p /home/appuser/.config/pulse && \
    printf '%s\n' \
      'default-sample-rate = 16000' \
      'alternate-sample-rate = 16000' \
      'default-sample-channels = 1' \
      'default-sample-format = s16le' \
      'resample-method = speex-float-5' \
      'flat-volumes = no' \
      > /home/appuser/.config/pulse/daemon.conf

RUN chown -R appuser:appuser /srv /opt/cloakbrowser /home/appuser/.config
USER appuser

# Default entrypoint stays entrypoint.sh so ECS (EC2/FARGATE) behaviour is
# byte-for-byte unchanged. The MicroVM image build overrides CMD to run the
# supervisor instead — it boots the same stack via entrypoint.sh STACK_ONLY,
# answers the lifecycle hooks, and spawns the app once /run delivers the
# per-meeting config. See src/microvm-supervisor.ts.
ENTRYPOINT ["/srv/entrypoint.sh"]
