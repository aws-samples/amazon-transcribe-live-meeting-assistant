#!/usr/bin/env bash
#
# install-macos.sh — one-shot installer for the LMA Audio Capture App (macOS).
#
# Ships inside the "Download Audio Capture App" zip from the LMA web UI. It:
#   1. checks/points you at prerequisites (Xcode command-line tools),
#   2. builds the Swift app in release mode,
#   3. wraps it in a signed .app bundle with its own TCC (privacy) identity,
#   4. leaves an lma-config.json (your deployment's endpoint + Cognito ids)
#      next to the executable so you only need to sign in with username/password.
#
# Usage:
#   ./install-macos.sh              # build into ./build/LMAAudioClient.app
#   ./install-macos.sh --run        # build, then launch (prompts for login)
#
# This is intentionally source-based: a macOS app using ScreenCaptureKit cannot
# be cross-compiled by the (Linux) LMA build pipeline, and Apple signing tools
# are macOS-only — so the app is built here, on your Mac. See README.md.
set -euo pipefail
cd "$(dirname "$0")"

RUN_AFTER=false
[[ "${1:-}" == "--run" ]] && RUN_AFTER=true

say() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
err() { printf "\n\033[31m✗ %s\033[0m\n" "$*" >&2; }

# ── 0. Clear download quarantine ─────────────────────────────────────────────
# When this package is downloaded via a browser, macOS tags every extracted file
# with com.apple.quarantine. Gatekeeper then blocks the build tools the script
# invokes with "Apple could not verify ... is free of malware". Since you have
# chosen to run this installer, strip the quarantine flag from this folder so the
# build can proceed. (You can inspect every file here — it is all plain source.)
if [[ "$(uname)" == "Darwin" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  # Run the clear UNCONDITIONALLY. Do NOT gate on `xattr -r -p`: it exits
  # non-zero whenever ANY node in the tree lacks the attribute — the normal case
  # when a browser download is unzipped with the CLI `unzip` (only files get
  # tagged, not directories). Gating therefore skipped the clear and the
  # Gatekeeper "cannot verify malware" popup kept firing. `xattr -dr` is safe and
  # exits 0 even on an already-clean tree.
  say "Clearing macOS download quarantine on this folder"
  xattr -dr com.apple.quarantine "$SCRIPT_DIR" 2>/dev/null || true
  echo "  • Quarantine cleared (this is what triggers Gatekeeper's download warning)"
fi

# ── 1. Prerequisites ─────────────────────────────────────────────────────────
say "Checking prerequisites"

if [[ "$(uname)" != "Darwin" ]]; then
  err "This installer is for macOS. (Windows/iOS/Android packages will live in their own platform folders.)"
  exit 1
fi

macos_major=$(sw_vers -productVersion | cut -d. -f1)
if (( macos_major < 13 )); then
  err "macOS 13 (Ventura) or later is required for ScreenCaptureKit audio capture. You have $(sw_vers -productVersion)."
  exit 1
fi
echo "  • macOS $(sw_vers -productVersion) ✓"

if ! xcode-select -p >/dev/null 2>&1; then
  err "Xcode command-line tools are not installed."
  echo "    Run this, complete the popup, then re-run this script:"
  echo "      xcode-select --install"
  exit 1
fi
echo "  • Xcode command-line tools ✓"

if ! command -v swift >/dev/null 2>&1; then
  err "The 'swift' compiler was not found even though command-line tools are installed."
  echo "    Try: sudo xcode-select --reset   (or install Xcode from the App Store)"
  exit 1
fi
echo "  • swift $(swift --version 2>/dev/null | head -1 | sed -E 's/.*version ([0-9.]+).*/\1/') ✓"

if [[ ! -f lma-config.json ]]; then
  err "lma-config.json is missing. This file (your LMA deployment's endpoint + Cognito ids)"
  echo "    is normally baked into the download zip. Without it you must pass --endpoint /"
  echo "    --user-pool-id / --client-id on the command line. Continuing anyway…"
else
  ep=$(grep -o '"wssEndpoint"[^,}]*' lma-config.json | sed -E 's/.*"wssEndpoint" *: *"([^"]*)".*/\1/')
  echo "  • lma-config.json ✓  (endpoint: ${ep:-unknown})"
fi

# ── 2. Build + bundle ────────────────────────────────────────────────────────
say "Building the app (this may take a minute on first run)"
# make-app.sh does the release build, .app assembly, bakes lma-config.json into
# Contents/Resources/, strips quarantine, and ad-hoc signs. (Config must NOT go
# in Contents/MacOS/ — codesign rejects non-code files there.)
./make-app.sh

APP="build/LMAAudioClient.app"
BIN="${APP}/Contents/MacOS/LMAAudioClient"
say "Done"
cat <<EOF
The app is built at:
  ${APP}

First run — grant permissions:
  1. Launch it (below). macOS will prompt for Microphone; approve it.
  2. Open System Settings ▸ Privacy & Security ▸ Screen Recording, enable
     "LMA Audio Client", then relaunch (Screen Recording needs a restart).
  3. It will ask for your LMA username and password (the same ones you use for
     the LMA web app), sign in, and start streaming.

Run it now with:
  ${BIN} --username you@example.com

EOF

if $RUN_AFTER; then
  say "Launching (you'll be prompted for username/password)"
  read -r -p "LMA username (email): " LMA_USER
  exec "${BIN}" --username "${LMA_USER}"
fi
