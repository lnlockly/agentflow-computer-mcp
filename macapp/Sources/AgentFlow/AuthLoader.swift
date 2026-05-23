import Foundation

/// Reads `~/.agentflow/auth.json` written by the Python CLI.
public enum AuthLoader {
    public static var defaultPath: URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent(".agentflow/auth.json")
    }

    /// Returns nil when the file does not exist or fails to parse.
    public static func load(from url: URL = defaultPath) -> AuthFile? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(AuthFile.self, from: data)
    }
}
