import Foundation

/// Wraps `agentflow daemon start|stop` invocations via `Process`.
public enum DaemonControl {
    /// Search order for the CLI binary.
    public static let searchPaths = [
        "/usr/local/bin/agentflow",
        "/opt/homebrew/bin/agentflow",
        NSString(string: "~/.local/bin/agentflow").expandingTildeInPath,
    ]

    public static func locateCLI() -> String? {
        for p in searchPaths where FileManager.default.isExecutableFile(atPath: p) {
            return p
        }
        return nil
    }

    public enum DaemonControlError: Error {
        case cliNotFound
        case spawnFailed(String)
    }

    /// Fires `agentflow daemon start` detached. Returns once spawned.
    public static func startDaemon() throws {
        try runDetached(args: ["daemon", "start"])
    }

    public static func restartDaemon() throws {
        // Best-effort stop, then start. We never error on stop because the
        // daemon may not be running, which is the explicit reason we restart.
        _ = try? runDetached(args: ["daemon", "stop"])
        try runDetached(args: ["daemon", "start"])
    }

    private static func runDetached(args: [String]) throws {
        guard let cli = locateCLI() else { throw DaemonControlError.cliNotFound }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: cli)
        proc.arguments = args
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
        } catch {
            throw DaemonControlError.spawnFailed(String(describing: error))
        }
    }
}
