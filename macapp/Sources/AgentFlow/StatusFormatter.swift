import Foundation

public enum StatusFormatter {
    /// "$1.23 / $5.00 (25%)". Caps below 0.01 render as "$0.00 / $X (0%)".
    public static func budget(spent: Double, cap: Double) -> String {
        let safeSpent = max(0, spent)
        let safeCap = max(0, cap)
        let s = String(format: "$%.2f", safeSpent)
        let c = String(format: "$%.2f", safeCap)
        let pct: Int
        if safeCap <= 0 {
            pct = 0
        } else {
            pct = Int((safeSpent / safeCap * 100).rounded())
        }
        return "\(s) / \(c) (\(pct)%)"
    }

    /// "3/5" given milestones, or "—" when nothing to count.
    public static func milestoneRatio(_ goal: Goal) -> String {
        guard let m = goal.milestones, !m.isEmpty else { return "—" }
        let done = m.filter { $0.done }.count
        return "\(done)/\(m.count)"
    }

    public static func percentLabel(_ goal: Goal) -> String {
        guard let p = goal.progress else { return "—" }
        return "\(Int((p * 100).rounded()))%"
    }

    /// Emoji + russian status label for an agent.
    public static func agentBadge(status: String) -> (emoji: String, label: String) {
        switch status.lowercased() {
        case "running", "busy":
            return ("🟢", "работает")
        case "idle":
            return ("⚪️", "ожидает")
        case "paused":
            return ("⏸", "пауза")
        case "error":
            return ("🔴", "ошибка")
        default:
            return ("•", status)
        }
    }
}
