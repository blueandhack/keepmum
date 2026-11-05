// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Keepmum",
    platforms: [.macOS(.v14)],
    dependencies: [
        // Updates (Updater.swift): the framework is embedded and signed by build-app.sh.
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.1"),
    ],
    targets: [
        .executableTarget(name: "Keepmum", dependencies: [.product(name: "Sparkle", package: "Sparkle")],
                          path: "Sources/Keepmum",
                          linkerSettings: [.unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"])]),
    ]
)
