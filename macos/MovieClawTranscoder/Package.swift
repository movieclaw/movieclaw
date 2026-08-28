// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "MovieClawTranscoder",
    platforms: [.macOS(.v12)],
    products: [
        .executable(name: "movieclaw-transcoder", targets: ["MovieClawTranscoder"]),
    ],
    targets: [
        .executableTarget(name: "MovieClawTranscoder"),
        .testTarget(
            name: "MovieClawTranscoderTests",
            dependencies: ["MovieClawTranscoder"]
        ),
    ]
)
