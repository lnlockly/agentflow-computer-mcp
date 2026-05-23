# AgentFlow Mac Menu-Bar App

SwiftUI status-bar app that shows the local agentflow daemon, cloud goals,
and today's budget. macOS 13+.

Spec: `docs/specs/2026-05-23-mac-menu-bar-app.md`.

## Build

```bash
cd macapp
swift build              # debug
swift build -c release   # production
swift test               # 28 unit tests
./scripts/build-app.sh   # assembles .build/AgentFlow.app
```

Bundle size after `build-app.sh`: ~420 KB.

## Run

Prereq: `agentflow` CLI installed (`install.sh` from repo root) and
`agentflow login` has been run so `~/.agentflow/auth.json` exists.

```bash
./scripts/build-app.sh
open .build/AgentFlow.app
```

The status-bar icon (lightning bolt) appears in the menu bar. Click it to see
agents + goals + budget. The Dock icon is suppressed via `LSUIElement=YES`.

## Gatekeeper note (until #77 ships codesign)

First launch on an unsigned binary will be blocked by macOS Gatekeeper. To
allow it once:

1. Right-click `.build/AgentFlow.app` in Finder.
2. Hold `Control` and click "Open".
3. Confirm the warning. macOS remembers the trust decision.

Once codesign + notarize lands (task #77), double-click works as normal.

## Manual smoke checklist

- [ ] Build succeeds: `swift build -c release`.
- [ ] All tests green: `swift test` reports 28/28 passing.
- [ ] `.app` bundle < 5 MB.
- [ ] Right-click → Open launches with no crash.
- [ ] Status bar shows lightning bolt; no Dock icon.
- [ ] With daemon running: header green dot, agents list populates within 10s.
- [ ] With daemon stopped: red dot, "Демон не запущен", "Запустить демон"
      button appears and is enabled if CLI is on PATH.
- [ ] "Открыть кабинет" opens https://agentflow.website/cabinet in browser.
- [ ] "Выйти" quits the app cleanly.
- [ ] Without `~/.agentflow/auth.json`: cloud sections show
      "не авторизован — agentflow login".

## Data sources

| Source | Endpoint | Cadence |
|---|---|---|
| Local agents | `/tmp/agentflow.sock` `{method:"list"}` | 10s |
| Cloud goals | `GET https://agentflow.website/_agents/me/autonomous/goals` | 30s |
| Cloud budget | `GET https://agentflow.website/_agents/me/autonomous/budget` | 30s |

All shapes match the Python CLI in `src/agentflow_computer_mcp/cli/`.

## Code layout

| File | Role |
|---|---|
| `Sources/AgentFlow/AgentFlowApp.swift` | `@main`, `MenuBarExtra` scene |
| `Sources/AgentFlow/AppState.swift` | `ObservableObject` with polling timers |
| `Sources/AgentFlow/Models.swift` | `Agent`, `Goal`, `Budget`, `AuthFile` |
| `Sources/AgentFlow/RestClient.swift` | REST against `/_agents` with `x-api-key` |
| `Sources/AgentFlow/SocketClient.swift` | AF_UNIX line-JSON one-shot client |
| `Sources/AgentFlow/AuthLoader.swift` | `~/.agentflow/auth.json` reader |
| `Sources/AgentFlow/DaemonControl.swift` | `Process` wrapper for `agentflow daemon` |
| `Sources/AgentFlow/StatusFormatter.swift` | pure formatters |
| `Sources/AgentFlow/MenuView.swift` | dropdown UI |
| `Sources/AgentFlow/Resources/Info.plist` | `LSUIElement`, bundle id |
| `Tests/AgentFlowTests/*.swift` | 28 XCTest cases |

## Sandbox + entitlements (future)

Currently unsandboxed. When codesign + sandbox land:

- `com.apple.security.network.client` — required for HTTPS REST.
- No `com.apple.security.app-sandbox` until we resolve `/tmp/agentflow.sock`
  access (sandbox blocks `/tmp/*.sock` reads by default).

## CI

Self-hosted Linux runner does not have Xcode, so the macOS gate is paid
`macos-latest`. PR is labelled `needs-mac-runner` to opt into that job — see
`.github/workflows/macapp.yml`.
