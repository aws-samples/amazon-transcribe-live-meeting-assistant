#!/usr/bin/env bash
#
# install-macos.sh — one-shot installer for the LMA Audio Capture App (macOS).
#
# Ships inside the "Download Audio Capture App" zip from the LMA web UI. It:
#   1. checks/points you at prerequisites (Xcode command-line tools),
#   2. builds the Swift app in release mode,
#   3. wraps it in a signed .app bundle with an lma-config.json (your
#      deployment's endpoint + Cognito ids) baked into Contents/Resources/,
#   4. installs it to /Applications so it's a first-class app — launchable from
#      Spotlight (Cmd-Space) and eligible for Start-at-login — with its own
#      privacy (TCC) identity ("LMA Audio Client").
#
# Usage (IMPORTANT — invoke with `bash`, not `./`):
#   bash install-macos.sh              # build + install to /Applications
#   bash install-macos.sh --run        # build + install, then launch it
#   bash install-macos.sh --no-install # build into ./build only (don't copy to /Applications)
#   INSTALL_DIR=~/Applications bash install-macos.sh   # override install location
#
# Why `bash install-macos.sh` and NOT `./install-macos.sh`:
#   On recent macOS, running a freshly downloaded script DIRECTLY (`./…`) makes
#   the kernel execve() a quarantined file, which Gatekeeper blocks — it SIGKILLs
#   the process and shows "Apple could not verify … is free of malware" BEFORE
#   line 1 runs, so this script's own quarantine-clearing never gets a chance.
#   `bash install-macos.sh` reads the script as DATA (no execve on a quarantined
#   file), so it runs, and then clears the quarantine flag from the rest of the
#   folder itself. (Equivalently: `xattr -dr com.apple.quarantine .` first.)
#
# This is intentionally source-based: a macOS app using ScreenCaptureKit cannot
# be cross-compiled by the (Linux) LMA build pipeline, and Apple signing tools
# are macOS-only — so the app is built here, on your Mac. See README.md.
set -euo pipefail
cd "$(dirname "$0")"

RUN_AFTER=false
NO_INSTALL=false
for arg in "$@"; do
  case "$arg" in
    --run) RUN_AFTER=true ;;
    --no-install) NO_INSTALL=true ;;
  esac
done

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
# Contents/Resources/, strips quarantine, and signs with a persistent local
# self-signed identity (created on first build; may prompt once for your login
# password to trust it). This keeps the Screen Recording permission valid
# across rebuilds — an ad-hoc signature would invalidate it every build.
# (Config must NOT go in Contents/MacOS/ — codesign rejects non-code files there.)
./make-app.sh

APP="build/LMAAudioClient.app"

# ── 3. Install into /Applications ────────────────────────────────────────────
# Installing to /Applications makes it a first-class app: launchable from
# Spotlight (Cmd-Space → "LMA Audio Client"), eligible for Start-at-login, and
# stable for macOS's privacy (TCC) records. Pass --no-install to skip and just
# leave the build in ./build. Set INSTALL_DIR to override the destination.
INSTALL_DIR="${INSTALL_DIR:-/Applications}"
INSTALLED_APP="${INSTALL_DIR}/LMAAudioClient.app"
if $NO_INSTALL; then
  INSTALLED_APP="$(cd "$(dirname "$APP")" && pwd)/$(basename "$APP")"
  say "Skipping install (--no-install); app left at ${INSTALLED_APP}"
else
  say "Installing to ${INSTALL_DIR}"
  rm -rf "${INSTALLED_APP}"
  if cp -R "${APP}" "${INSTALLED_APP}" 2>/dev/null; then
    xattr -dr com.apple.quarantine "${INSTALLED_APP}" 2>/dev/null || true
    # Refresh LaunchServices so Spotlight indexes it immediately.
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
      -f "${INSTALLED_APP}" 2>/dev/null || true
    echo "  • Installed: ${INSTALLED_APP}"

    # ── Pin to the Dock ──────────────────────────────────────────────────────
    # The Dock is the app's always-visible control surface (the menu-bar icon
    # can be hidden by the notch on crowded menu bars — worst of all at the
    # moment recording starts, when the system's orange mic indicator appears).
    # Append a persistent-apps entry and restart the Dock, unless it's already
    # pinned. Skip with SKIP_DOCK_PIN=1.
    if [[ "${SKIP_DOCK_PIN:-0}" != "1" ]]; then
      if defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "LMAAudioClient.app"; then
        echo "  • Already pinned to the Dock"
      else
        defaults write com.apple.dock persistent-apps -array-add "<dict>
          <key>tile-data</key><dict>
            <key>file-data</key><dict>
              <key>_CFURLString</key><string>file://${INSTALLED_APP}/</string>
              <key>_CFURLStringType</key><integer>15</integer>
            </dict>
          </dict>
        </dict>" 2>/dev/null \
          && killall Dock 2>/dev/null \
          && echo "  • Pinned to the Dock (the Dock will restart briefly)" \
          || echo "  • Couldn't pin to the Dock automatically — drag ${INSTALLED_APP} there manually"
      fi
    fi
  else
    INSTALLED_APP="$(cd "$(dirname "$APP")" && pwd)/$(basename "$APP")"
    err "Couldn't copy to ${INSTALL_DIR} (permission?). Leaving it at ${INSTALLED_APP}."
    echo "    To install manually: cp -R \"${INSTALLED_APP}\" /Applications/"
  fi
fi

say "Done"
cat <<EOF
LMA Audio Capture App is installed at:
  ${INSTALLED_APP}

▶ HOW TO LAUNCH IT (do NOT run it from Terminal):
  • Press Cmd-Space (Spotlight), type "LMA Audio Client", press Return, OR
  • double-click it in Finder / Launchpad, OR
  • run:  open -a "LMA Audio Client"

  Launching this way is REQUIRED: it goes through macOS LaunchServices so the
  app gets its own privacy identity. If you instead run the binary inside
  Contents/MacOS from Terminal, macOS attributes Microphone / Screen Recording
  to *Terminal* and system-audio capture will NOT work (you'd see "Terminal"
  in the recording indicator).

▶ FIRST RUN — grant permissions:
  1. Launch it (Dock icon, Spotlight, or Finder). A menu-bar item "LMA" appears
     (top-right) and the Dock icon shows a red dot + "REC" badge while
     recording. Approve the Microphone prompt.
  2. System Settings ▸ Privacy & Security ▸ Screen Recording → enable
     "LMA Audio Client". Then QUIT it (right-click the "LMA" menu-bar item ▸
     Quit) and launch it again — Screen Recording only takes effect after a
     relaunch. This is what lets it capture meeting/system audio.
  3. Left-click the "LMA" menu-bar item, sign in with your LMA username/password,
     and click Start.

▶ START AUTOMATICALLY AT LOGIN:
  Left-click the "LMA" menu-bar item and enable "Start automatically at login"
  (or System Settings ▸ General ▸ Login Items ▸ add LMA Audio Client).

EOF

if $RUN_AFTER; then
  say "Launching via Spotlight/LaunchServices (menu-bar app; sign in from the 'LMA' item)"
  open "${INSTALLED_APP}"
fi
