import Foundation

/// Local agent slot exposed by the daemon socket (`agents/socket.py:list`).
public struct Agent: Codable, Identifiable, Equatable {
    public let id: String
    public let name: String
    public let persona: String
    public let status: String

    public init(id: String, name: String, persona: String, status: String) {
        self.id = id
        self.name = name
        self.persona = persona
        self.status = status
    }
}

/// One milestone inside a cloud goal.
public struct Milestone: Codable, Equatable {
    public let title: String
    public let done: Bool

    public init(title: String, done: Bool) {
        self.title = title
        self.done = done
    }
}

/// Cloud goal from `/_agents/me/autonomous/goals`.
public struct Goal: Codable, Identifiable, Equatable {
    public let id: String
    public let title: String
    public let status: String?
    public let milestones: [Milestone]?

    public init(id: String, title: String, status: String? = nil, milestones: [Milestone]? = nil) {
        self.id = id
        self.title = title
        self.status = status
        self.milestones = milestones
    }

    /// 0.0 – 1.0. nil when no milestones declared yet.
    public var progress: Double? {
        guard let m = milestones, !m.isEmpty else { return nil }
        let done = m.filter { $0.done }.count
        return Double(done) / Double(m.count)
    }
}

/// Cloud budget from `/_agents/me/autonomous/budget`.
public struct Budget: Codable, Equatable {
    public let spentUsd: Double
    public let capUsd: Double

    private enum CodingKeys: String, CodingKey {
        case spentUsd = "spent_usd"
        case capUsd = "cap_usd"
    }

    public init(spentUsd: Double, capUsd: Double) {
        self.spentUsd = spentUsd
        self.capUsd = capUsd
    }
}

/// Auth bag mirroring `~/.agentflow/auth.json` written by the Python daemon.
public struct AuthFile: Codable, Equatable {
    public let apiKey: String?
    public let deviceId: String?
    public let wsUrl: String?

    private enum CodingKeys: String, CodingKey {
        case apiKey = "api_key"
        case deviceId = "device_id"
        case wsUrl = "ws_url"
    }
}

/// Wrappers — REST endpoints sometimes return `{ goals: [...] }` and sometimes a bare list.
struct GoalsEnvelope: Decodable {
    let goals: [Goal]?
    let items: [Goal]?
    let results: [Goal]?
}
