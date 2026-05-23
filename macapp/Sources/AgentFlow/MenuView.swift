import SwiftUI

struct MenuView: View {
    @ObservedObject var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            agentsSection
            Divider()
            goalsSection
            Divider()
            budgetSection
            Divider()
            actions
        }
        .frame(minWidth: 280)
    }

    private var header: some View {
        HStack {
            Circle()
                .fill(state.daemonOk ? Color.green : Color.red)
                .frame(width: 8, height: 8)
            Text(state.daemonOk ? "Подключено" : "Демон не запущен")
                .font(.headline)
            Spacer()
            if let upd = state.lastUpdated {
                Text(relativeTime(upd))
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var agentsSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Агенты")
                .font(.caption).bold()
                .foregroundColor(.secondary)
                .padding(.horizontal, 12)
                .padding(.top, 6)
            if state.agents.isEmpty {
                Text(state.daemonOk ? "пока пусто" : "—")
                    .font(.callout)
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 6)
            } else {
                ForEach(state.agents) { a in
                    let badge = StatusFormatter.agentBadge(status: a.status)
                    HStack {
                        Text(badge.emoji)
                        VStack(alignment: .leading, spacing: 0) {
                            Text(a.name.isEmpty ? a.id : a.name)
                                .font(.callout)
                            Text(badge.label)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                        Spacer()
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 2)
                }
                .padding(.bottom, 4)
            }
        }
    }

    private var goalsSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Цели")
                .font(.caption).bold()
                .foregroundColor(.secondary)
                .padding(.horizontal, 12)
                .padding(.top, 6)
            if !state.authPresent {
                Text("не авторизован — agentflow login")
                    .font(.callout)
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 6)
            } else if state.goals.isEmpty {
                Text("пока пусто")
                    .font(.callout)
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 6)
            } else {
                ForEach(state.goals) { g in
                    HStack {
                        VStack(alignment: .leading, spacing: 0) {
                            Text(g.title)
                                .font(.callout)
                                .lineLimit(1)
                            Text(StatusFormatter.milestoneRatio(g))
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                        Spacer()
                        Text(StatusFormatter.percentLabel(g))
                            .font(.caption)
                            .monospacedDigit()
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 2)
                }
                .padding(.bottom, 4)
            }
        }
    }

    private var budgetSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Бюджет")
                .font(.caption).bold()
                .foregroundColor(.secondary)
                .padding(.horizontal, 12)
                .padding(.top, 6)
            if let b = state.budget {
                Text(StatusFormatter.budget(spent: b.spentUsd, cap: b.capUsd))
                    .font(.callout)
                    .monospacedDigit()
                    .padding(.horizontal, 12)
                    .padding(.bottom, 6)
            } else {
                Text(state.authPresent ? "—" : "не авторизован")
                    .font(.callout)
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 6)
            }
        }
    }

    private var actions: some View {
        VStack(spacing: 0) {
            Button("Открыть кабинет") {
                if let url = URL(string: "https://agentflow.website/cabinet") {
                    NSWorkspace.shared.open(url)
                }
            }
            .buttonStyle(.plain)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)

            Button("Перезапустить демон") {
                try? DaemonControl.restartDaemon()
                Task { await state.refreshLocal() }
            }
            .buttonStyle(.plain)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)

            if !state.daemonOk {
                Button("Запустить демон") {
                    try? DaemonControl.startDaemon()
                    Task { await state.refreshLocal() }
                }
                .buttonStyle(.plain)
                .disabled(DaemonControl.locateCLI() == nil)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
            }

            Divider()

            Button("Выйти") {
                NSApplication.shared.terminate(nil)
            }
            .buttonStyle(.plain)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
        }
    }

    private func relativeTime(_ d: Date) -> String {
        let s = Int(-d.timeIntervalSinceNow)
        if s < 60 { return "\(s)s ago" }
        if s < 3600 { return "\(s / 60)m ago" }
        return "\(s / 3600)h ago"
    }
}
