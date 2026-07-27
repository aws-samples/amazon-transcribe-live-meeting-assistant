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

# Generate the Dock icon (AppIcon.icns) — required now that the app is a
# regular Dock app (not LSUIElement). Rendered at build time from the same
# SF Symbol the menu bar uses, so there's no binary asset in the repo. Swift
# is guaranteed here (we just built with it); iconutil ships with macOS.
echo "==> generating AppIcon.icns"
ICONSET_DIR="build/AppIcon.iconset"
rm -rf "${ICONSET_DIR}"
mkdir -p "${ICONSET_DIR}"
swift - "${ICONSET_DIR}" <<'EOF'
import AppKit
let outDir = CommandLine.arguments[1]
// LMA brand-ish: white waveform glyph on a rounded dark-blue tile.
for size in [16, 32, 64, 128, 256, 512, 1024] {
    let s = CGFloat(size)
    let img = NSImage(size: NSSize(width: s, height: s))
    img.lockFocus()
    let inset = s * 0.05 // macOS icons float inside a small margin
    let tile = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    NSColor(calibratedRed: 0.09, green: 0.19, blue: 0.32, alpha: 1).setFill() // AWS squid ink
    NSBezierPath(roundedRect: tile, xRadius: s * 0.18, yRadius: s * 0.18).fill()
    let cfg = NSImage.SymbolConfiguration(pointSize: s * 0.5, weight: .medium)
        .applying(.init(paletteColors: [.white]))
    if let sym = NSImage(systemSymbolName: "waveform", accessibilityDescription: nil)?
        .withSymbolConfiguration(cfg) {
        let r = NSRect(x: (s - sym.size.width) / 2, y: (s - sym.size.height) / 2,
                       width: sym.size.width, height: sym.size.height)
        sym.draw(in: r)
    }
    img.unlockFocus()
    if let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
       let png = rep.representation(using: .png, properties: [:]) {
        // Emit both scales per iconset naming convention where applicable.
        let names: [String] = {
            switch size {
            case 16: return ["icon_16x16.png"]
            case 32: return ["icon_16x16@2x.png", "icon_32x32.png"]
            case 64: return ["icon_32x32@2x.png"]
            case 128: return ["icon_128x128.png"]
            case 256: return ["icon_128x128@2x.png", "icon_256x256.png"]
            case 512: return ["icon_256x256@2x.png", "icon_512x512.png"]
            default: return ["icon_512x512@2x.png"]
            }
        }()
        for n in names { try? png.write(to: URL(fileURLWithPath: "\(outDir)/\(n)")) }
    }
}
EOF
iconutil -c icns "${ICONSET_DIR}" -o "${RESOURCES_DIR}/AppIcon.icns"
rm -rf "${ICONSET_DIR}"
echo "    baked AppIcon.icns into Contents/Resources/"

# Strip any quarantine the copied inputs carried in (macOS `cp` preserves the
# com.apple.quarantine xattr), so the assembled bundle is clean before signing
# and won't trip Gatekeeper when launched.
xattr -dr com.apple.quarantine "${APP_DIR}" 2>/dev/null || true

# Sign the bundle with a PERSISTENT SELF-SIGNED identity (created on first
# build), NOT ad-hoc. Why this matters: an ad-hoc signature has no identity, so
# its designated requirement is a literal cdhash of that exact binary — and TCC
# binds the Screen Recording grant to it. Every rebuild produces a new cdhash,
# invalidating the grant: System Settings still SHOWS the app enabled, but
# macOS re-prompts on every capture. A certificate-backed signature (even
# self-signed) anchors the designated requirement to the cert identity, so the
# grant survives rebuilds on this machine. (An Apple Developer ID would also
# fix Gatekeeper warnings; this fixes the TCC re-prompt loop without one.)
SIGN_IDENTITY="LMA Audio Client Local Signing"

ensure_signing_identity() {
  # Already have a usable identity? Done. (find-identity only lists certs that
  # are valid for codesigning AND have their private key in the keychain.)
  if security find-identity -v -p codesigning 2>/dev/null | grep -q "${SIGN_IDENTITY}"; then
    return 0
  fi
  echo "==> creating one-time self-signed codesigning certificate: ${SIGN_IDENTITY}"
  # Generate the cert + key with OpenSSL, then import into the login keychain.
  # codesign requires the extendedKeyUsage=codeSigning extension.
  local tmp
  tmp="$(mktemp -d)"
  openssl req -x509 -newkey rsa:2048 -keyout "${tmp}/key.pem" -out "${tmp}/cert.pem" \
    -days 3650 -nodes -subj "/CN=${SIGN_IDENTITY}" \
    -addext "extendedKeyUsage=codeSigning" \
    -addext "keyUsage=digitalSignature" 2>/dev/null
  # OpenSSL 3.x defaults PKCS#12 to AES/PBKDF2, which `security import` can't
  # extract the PRIVATE KEY from — the cert imports but no identity forms
  # ("0 valid identities found"). -legacy restores compatible encryption.
  # LibreSSL (/usr/bin/openssl) has no -legacy flag but its defaults are
  # already compatible, so fall back to plain -export if -legacy is unknown.
  if ! openssl pkcs12 -export -legacy -out "${tmp}/identity.p12" \
      -inkey "${tmp}/key.pem" -in "${tmp}/cert.pem" \
      -name "${SIGN_IDENTITY}" -passout pass:lma-local 2>/dev/null; then
    openssl pkcs12 -export -out "${tmp}/identity.p12" \
      -inkey "${tmp}/key.pem" -in "${tmp}/cert.pem" \
      -name "${SIGN_IDENTITY}" -passout pass:lma-local 2>/dev/null
  fi
  local login_keychain
  login_keychain="$(security default-keychain -d user | tr -d ' "')"
  security import "${tmp}/identity.p12" -k "${login_keychain}" -P lma-local \
    -T /usr/bin/codesign -T /usr/bin/security
  # Trust our own cert for code signing so codesign doesn't reject it as
  # untrusted (may prompt once for your login password — expected, one time).
  security add-trusted-cert -p codeSign -k "${login_keychain}" "${tmp}/cert.pem" || true
  # Let codesign use the private key without a per-build password prompt.
  security set-key-partition-list -S apple-tool:,apple: -s -k "" "${login_keychain}" 2>/dev/null || true
  rm -rf "${tmp}"
  # Verify the identity actually formed (cert + private key + trust).
  security find-identity -v -p codesigning 2>/dev/null | grep -q "${SIGN_IDENTITY}"
}

if [[ "${LMA_ADHOC_SIGN:-0}" == "1" ]]; then
  # Escape hatch (CI, throwaway builds): LMA_ADHOC_SIGN=1 restores old behavior.
  echo "==> ad-hoc codesign (LMA_ADHOC_SIGN=1)"
  codesign --force --sign - \
    --entitlements LMAAudioClient.entitlements \
    --options runtime \
    "${APP_DIR}"
elif ensure_signing_identity; then
  echo "==> codesign with persistent identity: ${SIGN_IDENTITY}"
  if ! codesign --force --sign "${SIGN_IDENTITY}" \
      --entitlements LMAAudioClient.entitlements \
      --options runtime \
      "${APP_DIR}"; then
    echo "    WARNING: identity signing failed — falling back to ad-hoc."
    echo "    (Screen Recording permission will need re-granting after each rebuild.)"
    codesign --force --sign - \
      --entitlements LMAAudioClient.entitlements \
      --options runtime \
      "${APP_DIR}"
  fi
else
  echo "    WARNING: couldn't create signing identity — falling back to ad-hoc."
  echo "    (Screen Recording permission will need re-granting after each rebuild.)"
  codesign --force --sign - \
    --entitlements LMAAudioClient.entitlements \
    --options runtime \
    "${APP_DIR}"
fi

echo ""
echo "Built ${APP_DIR}"
echo "Run it with:"
echo "  ${MACOS_DIR}/${APP_NAME} \\"
echo "    --endpoint wss://<cloudfront-domain>/api/v1/ws \\"
echo "    --token <cognito-access-token> --id-token <cognito-id-token> \\"
echo "    --call-id \"Native Mac test\""
