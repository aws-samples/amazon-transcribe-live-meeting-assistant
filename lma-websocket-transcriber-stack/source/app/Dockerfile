# checkov:skip=CKV_DOCKER_2: "Health monitored by ECS target-group health check on /health/check endpoint rather than container HEALTHCHECK"
ARG NODE_VERSION=20

# First, build the project
FROM public.ecr.aws/docker/library/node:20-bookworm AS builder

WORKDIR /app

COPY [\
    "./eslint.config.mjs",\
    "./package.json",\
    "./package-lock.json",\
    "./tsconfig.json",\
    "/app/"\
]

RUN npm ci

COPY "./src" "/app/src"

RUN npm run build

RUN npm prune --production

# Now create the runtime image and copy the build artifacts into it
FROM public.ecr.aws/docker/library/node:20-slim AS runtime

# Apply all pending security patches to resolve Mirador/ECR container findings.
# ffmpeg is installed here (before apt is purged below) — used to mux the
# optional desktop-video stream with the call audio into a single MP4.
# NOTE: ffmpeg's Debian build links the gnutls stack (libgnutls30, libnettle8,
# libhogweed6, libp11-kit0, libtasn1-6) and, via libssh-gcrypt, libgcrypt20 +
# libgpg-error0 — so those libraries, previously purged outright, are now
# retained at their fully-patched (post-upgrade) versions. The rest of the
# hardening purge (systemd, apt itself, gpgv) stands.
RUN set -eux; \
    apt-get update; \
    apt-get -y upgrade; \
    apt-get -y install --no-install-recommends ffmpeg; \
    apt-get -y --purge remove systemd libsystemd-shared || true; \
    apt-get -y autoremove --purge || true; \
    apt-get -y clean || true; \
    dpkg --purge --force-remove-essential --force-depends \
        apt apt-utils libapt-pkg6.0 \
        gpgv \
        || true; \
    rm -rf /var/lib/apt /var/lib/dpkg/info/apt.* /var/lib/dpkg/info/libapt-pkg*.* \
           /var/cache/apt /var/cache/debconf/* \
           /usr/bin/apt /usr/bin/apt-* /usr/bin/gpgv \
           /usr/share/doc/apt /usr/share/doc/libapt-pkg* \
           2>/dev/null || true

WORKDIR /app
ENV NODE_ENV=production
ENV LOG_ROOT_DIR=/tmp/
ENV SERVERPORT=8080
ENV SERVERHOST=0.0.0.0
EXPOSE $SERVERPORT

COPY --from=builder "/app/node_modules/" "/app/node_modules"
COPY --from=builder "/app/dist" "/app/dist"

USER node
ENTRYPOINT ["node", "/app/dist/index.js"]
