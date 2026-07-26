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
