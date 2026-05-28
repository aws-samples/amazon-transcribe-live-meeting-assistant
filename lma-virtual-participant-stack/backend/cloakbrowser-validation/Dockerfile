# =============================================================================
# CloakBrowser Validation Dockerfile
# -----------------------------------------------------------------------------
# Minimal image that:
#   1. Installs the CloakBrowser stealth Chromium binary (via the cloakbrowser
#      Python package, which auto-downloads the binary on first launch).
#   2. Installs Node.js + the cloakbrowser npm package + playwright-core so we
#      can drive the browser with the same JS/TS Playwright API used by the
#      existing virtual participant backend.
#   3. Runs Xvfb + x11vnc + noVNC so we can watch the browser over VNC for
#      manual validation.
#   4. Launches a tiny Playwright test script that opens a single page and
#      keeps the browser alive so we can inspect it via VNC.
#
# This is a STANDALONE validation image — it does NOT replace the production
# Dockerfile. Build/run instructions are in this folder's README.md.
# =============================================================================

# CloakBrowser distributes a glibc Chromium binary, so we need a glibc base
# (Debian/Ubuntu). The cloakbrowser-manager reference Dockerfile uses
# python:3.12-slim — we follow the same pattern.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    SCREEN_WIDTH=1920 \
    SCREEN_HEIGHT=1080 \
    SCREEN_DEPTH=24 \
    VNC_PORT=5900 \
    NOVNC_PORT=5901 \
    # Tell cloakbrowser where to cache the binary (also where ensureBinary writes)
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser \
    # Default OFF: deterministic builds. Set AUTO_UPDATE=1 at `docker run` time
    # to upgrade pip + chromium to the latest at container startup. We default
    # OFF because the latest (0.3.30 + Chromium .5) regressed Zoom detection
    # vs the Manager-image-bundled 0.3.26 + .3 — we want the *known good*
    # versions on every fresh run, with opt-in escape hatch to track upstream.
    AUTO_UPDATE=0 \
    # Belt-and-braces: also tell the cloakbrowser library itself NOT to do its
    # own background update check on launch. Without this, even if AUTO_UPDATE=0,
    # the library will silently download a newer Chromium in the background
    # ("Downloading in background..." in the Manager logs) and use it on the
    # *next* launch — defeating the version pin. We turn this off here, then
    # the entrypoint flips it on only when AUTO_UPDATE=1.
    CLOAKBROWSER_AUTO_UPDATE=false

# -----------------------------------------------------------------------------
# 1. System packages: Chromium runtime deps, Xvfb, VNC, fonts, Node.js
# -----------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Chromium runtime libs (lifted from cloakbrowser-manager Dockerfile)
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
        libxcb1 libxext6 libxshmfence1 \
        libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
        libgdk-pixbuf-2.0-0 libxss1 libxtst6 fonts-liberation \
        libgl1-mesa-dri libegl-mesa0 \
        # Display + VNC stack
        xvfb x11vnc fluxbox novnc websockify \
        # Anti-bot canvas font hashes need real emoji/CJK fonts on Linux
        fonts-noto-color-emoji fonts-freefont-ttf fonts-unifont \
        fonts-ipafont-gothic fonts-wqy-zenhei \
        # Tools
        procps wget curl ca-certificates net-tools git \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (matches the production virtual participant backend)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# 2. Install cloakbrowser (Python) and pre-download the stealth Chromium binary.
#
#    PIN the wrapper version to match the `cloakhq/cloakbrowser-manager` image,
#    which is known to pass Zoom/Cloudflare out of the box.
#
#    Important: the cloakbrowser wrapper hard-codes the Chromium binary version
#    inside its config.py (PLATFORM_CHROMIUM_VERSIONS), so pinning the pip
#    version automatically pins the Chromium binary version. Verified by
#    inspecting `cloakbrowser==0.3.26`'s config.py:
#       linux-x64 → "146.0.7680.177.3"   ← matches the Manager image
#
#    DO NOT set CLOAKBROWSER_DOWNLOAD_URL here — the library treats it as a
#    base URL/release tag prefix and appends "/chromium-v<version>/..." on
#    its own, which leads to a 404 if you supply a full URL.
# -----------------------------------------------------------------------------
ARG CLOAKBROWSER_VERSION=0.3.26

# Note: the Manager declares `cloakbrowser[geoip]>=0.3.14` (with the geoip
# extra). We install the geoip extra too so MODE=manager has the same
# transitive deps as a real Manager container.
RUN pip install --no-cache-dir "cloakbrowser[geoip]==${CLOAKBROWSER_VERSION}" \
    && python -m cloakbrowser install \
    && python -m cloakbrowser info

# -----------------------------------------------------------------------------
# 2b. Clone the CloakBrowser-Manager source so MODE=manager can import its
#     `BrowserManager` class directly. This is the closest possible match to
#     running the official Manager image — it imports the EXACT same Python
#     files (browser_manager.py, database.py, vnc_manager.py, etc.) that
#     run inside `cloakhq/cloakbrowser-manager`.
#
#     Pinned to a tag so the build is reproducible. Bump CLOAKBROWSER_MANAGER_REF
#     to track upstream when needed.
# -----------------------------------------------------------------------------
ARG CLOAKBROWSER_MANAGER_REF=v0.0.10
RUN git clone --depth 1 --branch "${CLOAKBROWSER_MANAGER_REF}" \
        https://github.com/CloakHQ/CloakBrowser-Manager.git \
        /opt/cloakbrowser-manager \
    && pip install --no-cache-dir -r /opt/cloakbrowser-manager/backend/requirements.txt

# -----------------------------------------------------------------------------
# 3. Install cloakbrowser (npm) + playwright-core for the JS validation script
# -----------------------------------------------------------------------------
WORKDIR /app
COPY package.json /app/package.json
RUN npm install --no-audit --no-fund

# -----------------------------------------------------------------------------
# 4. Validation script + entrypoint
# -----------------------------------------------------------------------------
# `lib/` holds our JS port of the CloakBrowser-Manager profile-launch glue
# (browser_manager.py + database.py → profile.mjs). Both validate-* scripts
# import from here. See lib/profile.mjs for the full porting rationale.
COPY lib/                    /app/lib/
COPY validate-playwright.mjs /app/validate-playwright.mjs
COPY validate-puppeteer.mjs  /app/validate-puppeteer.mjs
# validate-python.py uses the Python cloakbrowser package directly (same
# import path the Manager uses). Set MODE=python at run time to use it
# instead of Playwright/Puppeteer — useful for A/B testing the JS wrapper
# vs the Python wrapper as the stealth-effectiveness variable.
COPY validate-python.py      /app/validate-python.py
# validate-manager.py imports the Manager's REAL BrowserManager class from
# /opt/cloakbrowser-manager (cloned in step 2b) and calls its launch() method
# directly. Set MODE=manager to use it. This is the closest-to-Manager-image
# code path possible without running the Manager image itself.
COPY validate-manager.py     /app/validate-manager.py
COPY entrypoint.sh           /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 5900 = raw VNC, 5901 = noVNC over WebSocket (browse to http://host:5901/vnc.html)
EXPOSE 5900 5901

# Persistent profile storage. Mount a host directory or a named volume here
# (e.g. `-v cloakprofiles:/data`) to keep cookies, localStorage, and
# fingerprint-derived files across container restarts. Without a mount the
# profile lives only inside the container and is lost on `docker rm`.
VOLUME /data

ENTRYPOINT ["/app/entrypoint.sh"]
