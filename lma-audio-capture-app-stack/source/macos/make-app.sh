#!/usr/bin/env bash
#
# make-app.sh — wrap the release binary in a minimal .app bundle so the client
# gets its OWN TCC (privacy) identity instead of inheriting Terminal's.
#
# Why: a bare `swift run` executable has no bundle/Info.plist, so macOS
# attributes Microphone + Screen Recording to the parent process (Terminal).
# A .app with an Info.plist (NSMicrophoneUsageDescription) is treated as its
# own app by TCC — the permission prompts and System Settings toggles then say
# "LMAAudioClient", which is how the eventual production build behaves too.
#
# Usage:
#   ./make-app.sh                 # build release + assemble ./build/LMAAudioClient.app
#   ./build/LMAAudioClient.app/Contents/MacOS/LMAAudioClient --endpoint ... --token ...
#
# Re-run after any code change. The bundle is ad-hoc signed (required on Apple
# Silicon just to execute); that is NOT Apple notarization — fine for local use.
set -euo pipefail

cd "$(dirname "$0")"
APP_NAME="LMAAudioClient"
APP_DIR="build/${APP_NAME}.app"
MACOS_DIR="${APP_DIR}/Contents/MacOS"
RESOURCES_DIR="${APP_DIR}/Contents/Resources"

echo "==> swift build -c release"
swift build -c release
BIN_PATH="$(swift build -c release --show-bin-path)/${APP_NAME}"

echo "==> assembling ${APP_DIR}"
rm -rf "${APP_DIR}"
mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}"
cp "${BIN_PATH}" "${MACOS_DIR}/${APP_NAME}"
cp Info.plist "${APP_DIR}/Contents/Info.plist"

# Bake the deployment config into Contents/Resources/ (NOT Contents/MacOS/).
# codesign REJECTS any non-code file placed alongside the executable in MacOS/
# ("code object is not signed at all"), which aborted this script under
# `set -e` and left the app unsigned. Config.swift's loadConfigFile() already
# looks in ../Resources/ relative to the executable, so no code change needed.
if [[ -f lma-config.json ]]; then
  cp lma-config.json "${RESOURCES_DIR}/lma-config.json"
  echo "    baked lma-config.json into Contents/Resources/"
else
  echo "    WARNING: no lma-config.json to bake — the app will have no endpoint/pool/client"
fi

# Strip any quarantine the copied inputs carried in (macOS `cp` preserves the
# com.apple.quarantine xattr), so the assembled bundle is clean before signing
# and won't trip Gatekeeper when launched.
xattr -dr com.apple.quarantine "${APP_DIR}" 2>/dev/null || true

# Ad-hoc sign the bundle with entitlements. On Apple Silicon a signature is
# mandatory just to run; the entitlements match what a notarized build needs.
echo "==> ad-hoc codesign (with entitlements)"
codesign --force --sign - \
  --entitlements LMAAudioClient.entitlements \
  --options runtime \
  "${APP_DIR}"

echo ""
echo "Built ${APP_DIR}"
echo "Run it with:"
echo "  ${MACOS_DIR}/${APP_NAME} \\"
echo "    --endpoint wss://<cloudfront-domain>/api/v1/ws \\"
echo "    --token <cognito-access-token> --id-token <cognito-id-token> \\"
echo "    --call-id \"Native Mac test\""
