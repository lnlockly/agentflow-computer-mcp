// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "AgentFlow",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "AgentFlow", targets: ["AgentFlow"])
    ],
    targets: [
        .executableTarget(
            name: "AgentFlow",
            path: "Sources/AgentFlow",
            exclude: ["Resources/Info.plist"]
        ),
        .testTarget(
            name: "AgentFlowTests",
            dependencies: ["AgentFlow"],
            path: "Tests/AgentFlowTests"
        )
    ]
)
