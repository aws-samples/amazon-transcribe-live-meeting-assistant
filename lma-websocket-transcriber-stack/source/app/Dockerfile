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

# Static ffmpeg builder. Produces ONE self-contained binary with only the
# features the mux needs, so the runtime image keeps its full security purge
# (see the note in the runtime stage).
#
# The source comes from Debian's own ffmpeg SOURCE PACKAGE via `apt-get source`,
# NOT from ffmpeg.org. That matters: fetching from ffmpeg.org failed in
# CodeBuild ("Failed to connect to ffmpeg.org port 443 after 131355 ms"), because
# the build environment can reach the configured Debian mirror but not arbitrary
# third-party hosts. Using the same mirror apt already uses keeps the build
# self-consistent, avoids a single-host dependency, and pins the version to the
# distro's (patched) release.
FROM public.ecr.aws/docker/library/debian:bookworm-slim AS ffmpeg-build
RUN set -eux; \
    sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
      || printf 'deb-src http://deb.debian.org/debian bookworm main\n' >> /etc/apt/sources.list; \
    apt-get update; \
    apt-get -y install --no-install-recommends \
        build-essential dpkg-dev nasm pkg-config xz-utils; \
    mkdir -p /src && cd /src; \
    apt-get source ffmpeg; \
    cd "$(find /src -maxdepth 1 -type d -name 'ffmpeg-*' | head -1)"; \
    # --disable-everything, then enable ONLY what the mux uses: read fragmented
    # MP4 + WAV, write MP4, copy H.264, encode AAC. No network protocols and no
    # external libs, so the binary links none of the purged gnutls/gcrypt stack.
    ./configure \
        --disable-everything \
        --disable-shared --enable-static \
        --disable-doc --disable-debug \
        --disable-network --disable-autodetect \
        --enable-protocol=file,pipe \
        --enable-demuxer=mov,mp4,m4v,wav,concat \
        --enable-muxer=mp4 \
        --enable-decoder=h264,aac,pcm_s16le \
        --enable-encoder=aac \
        --enable-parser=h264,aac \
        --enable-bsf=h264_mp4toannexb,extract_extradata \
        --enable-filter=aresample,anull,null \
        --disable-ffplay --disable-ffprobe; \
    make -j"$(nproc)"; \
    strip ffmpeg; \
    cp ffmpeg /usr/local/bin/ffmpeg; \
    /usr/local/bin/ffmpeg -version

# Now create the runtime image and copy the build artifacts into it
FROM public.ecr.aws/docker/library/node:20-slim AS runtime

# Apply all pending security patches to resolve Mirador/ECR container findings.
#
# The ORIGINAL hardening purge is restored in full here (including libgnutls30,
# libgcrypt20, libnettle8, libhogweed6, libp11-kit0, libtasn1-6, libgpg-error0)
# because ffmpeg is NOT installed from apt. Installing Debian's ffmpeg would
# link that whole gnutls/gcrypt stack — the recurring high-severity CVE surface
# this purge exists to remove — and pull ~250 MB of codecs, SDL2, Vulkan, etc.
# into an internet-facing container that only needs a stream copy plus one AAC
# encode. Instead a STATIC ffmpeg is built in the stage above and copied in as a
# single self-contained binary: no shared-library footprint, no CVE surface, and
# the purge below can stay exactly as it was before video support existed.
RUN set -eux; \
    apt-get update; \
    apt-get -y upgrade; \
    apt-get -y --purge remove systemd libsystemd-shared || true; \
    apt-get -y autoremove --purge || true; \
    apt-get -y clean || true; \
    dpkg --purge --force-remove-essential --force-depends \
        apt apt-utils libapt-pkg6.0 \
        libgnutls30 libhogweed6 libnettle8 libp11-kit0 libtasn1-6 \
        libgcrypt20 libgpg-error0 \
        gpgv \
        || true; \
    rm -rf /var/lib/apt /var/lib/dpkg/info/apt.* /var/lib/dpkg/info/libapt-pkg*.* \
           /var/cache/apt /var/cache/debconf/* \
           /usr/lib/*/libgnutls.so.* /usr/lib/*/libgcrypt.so.* \
           /usr/lib/*/libhogweed.so.* /usr/lib/*/libnettle.so.* \
           /usr/lib/*/libp11-kit.so.* /usr/lib/*/libtasn1.so.* \
           /usr/lib/*/libgpg-error.so.* \
           /usr/bin/apt /usr/bin/apt-* /usr/bin/gpgv \
           /usr/share/doc/libgnutls30 /usr/share/doc/libgcrypt20 \
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
# Static, dependency-free ffmpeg for the end-of-call video mux (see note above).
COPY --from=ffmpeg-build /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg

USER node
ENTRYPOINT ["node", "/app/dist/index.js"]
