import Foundation
import Combine

@MainActor
public final class AppState: ObservableObject {
    @Published public var daemonOk: Bool = false
    @Published public var authPresent: Bool = false
    @Published public var agents: [Agent] = []
    @Published public var goals: [Goal] = []
    @Published public var budget: Budget?
    @Published public var lastError: String?
    @Published public var lastUpdated: Date?

    private let socket: SocketClient
    private let rest: RestClient
    private var pollTask: Task<Void, Never>?

    public init(
        socket: SocketClient = SocketClient(),
        rest: RestClient = RestClient.defaultClient()
    ) {
        self.socket = socket
        self.rest = rest
        self.authPresent = AuthLoader.load()?.apiKey != nil
    }

    public func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            guard let self else { return }
            var tick = 0
            while !Task.isCancelled {
                await self.refreshLocal()
                if tick % 3 == 0 {
                    await self.refreshRemote()
                }
                tick += 1
                try? await Task.sleep(nanoseconds: 10_000_000_000) // 10s
            }
        }
    }

    public func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    /// Pulls fresh local-agent snapshot from the daemon socket.
    public func refreshLocal() async {
        do {
            let agents = try await Task.detached(priority: .utility) { [socket] in
                try socket.listAgents()
            }.value
            self.agents = agents
            self.daemonOk = true
            self.lastUpdated = Date()
        } catch {
            self.daemonOk = false
            self.agents = []
        }
    }

    /// Refreshes cloud goals + budget.
    public func refreshRemote() async {
        // Re-read auth on every cycle so the user can `agentflow login`
        // and have the app pick it up without restarting.
        let auth = AuthLoader.load()
        self.authPresent = (auth?.apiKey?.isEmpty == false)
        guard authPresent else {
            self.goals = []
            self.budget = nil
            return
        }
        do {
            async let g = rest.fetchGoals()
            async let b = rest.fetchBudget()
            self.goals = try await g
            self.budget = try await b
            self.lastError = nil
            self.lastUpdated = Date()
        } catch {
            self.lastError = String(describing: error)
        }
    }
}
