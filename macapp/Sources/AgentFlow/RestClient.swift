import Foundation

public enum RestError: Error, Equatable {
    case notAuthenticated
    case http(Int, String)
    case decode(String)
    case network(String)
}

/// REST client for `https://agentflow.website/_agents`.
public final class RestClient {
    public static let defaultBase = URL(string: "https://agentflow.website/_agents")!

    private let base: URL
    private let session: URLSession
    private let apiKeyProvider: () -> String?

    public init(
        base: URL = RestClient.defaultBase,
        session: URLSession = .shared,
        apiKey: @escaping () -> String?
    ) {
        self.base = base
        self.session = session
        self.apiKeyProvider = apiKey
    }

    /// Convenience: build a default-config client reading `~/.agentflow/auth.json`.
    public static func defaultClient() -> RestClient {
        RestClient { AuthLoader.load()?.apiKey }
    }

    // MARK: - public API

    public func fetchGoals() async throws -> [Goal] {
        let data = try await get(path: "/me/autonomous/goals")
        // The server returns either `{"goals":[...]}` or `[...]`. Handle both.
        if let env = try? JSONDecoder().decode(GoalsEnvelope.self, from: data),
           let goals = env.goals ?? env.items ?? env.results {
            return goals
        }
        if let arr = try? JSONDecoder().decode([Goal].self, from: data) {
            return arr
        }
        throw RestError.decode("goals payload did not match list or envelope shape")
    }

    public func fetchBudget() async throws -> Budget {
        let data = try await get(path: "/me/autonomous/budget")
        do {
            return try JSONDecoder().decode(Budget.self, from: data)
        } catch {
            throw RestError.decode("budget: \(error)")
        }
    }

    // MARK: - transport

    func get(path: String) async throws -> Data {
        guard let key = apiKeyProvider(), !key.isEmpty else {
            throw RestError.notAuthenticated
        }
        let url = base.appendingPathComponent(path.hasPrefix("/") ? String(path.dropFirst()) : path)
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue(key, forHTTPHeaderField: "x-api-key")
        req.setValue("agentflow-macapp/1", forHTTPHeaderField: "user-agent")
        req.timeoutInterval = 15

        let (data, resp): (Data, URLResponse)
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw RestError.network(String(describing: error))
        }
        guard let http = resp as? HTTPURLResponse else {
            throw RestError.network("non-HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw RestError.http(http.statusCode, body)
        }
        return data
    }
}
