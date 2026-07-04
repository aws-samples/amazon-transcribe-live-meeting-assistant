// swift-tools-version:5.9
import PackageDescription

// LMA native macOS audio client — PROTOTYPE / spike.
// Dependency-free on purpose: everything below is a system framework
// (Foundation / AVFoundation / ScreenCaptureKit / CoreMedia), so this
// builds offline with just the Xcode command-line tools installed.
//
// ScreenCaptureKit audio capture requires macOS 13 (Ventura) or later.
let package = Package(
    name: "LMAAudioClient",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "LMAAudioClient",
            path: "Sources/LMAAudioClient"
        )
    ]
)
