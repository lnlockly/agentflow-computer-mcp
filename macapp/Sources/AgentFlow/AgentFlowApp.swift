import SwiftUI

@main
struct AgentFlowApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        MenuBarExtra {
            MenuView(state: state)
                .onAppear {
                    state.startPolling()
                    Task {
                        await state.refreshLocal()
                        await state.refreshRemote()
                    }
                }
        } label: {
            Image(systemName: "bolt.fill")
        }
        .menuBarExtraStyle(.window)
    }
}
