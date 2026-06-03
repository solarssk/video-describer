// swift-tools-version:5.5
// This package exists solely so that CodeQL can analyse macos_path_picker.swift.
// The tool itself is compiled directly with `swiftc` at runtime on macOS.
import PackageDescription

let package = Package(
    name: "video-describer-tools",
    platforms: [.macOS(.v11)],
    targets: [
        .executableTarget(
            name: "macos-path-picker",
            path: "tools"
        ),
    ]
)
