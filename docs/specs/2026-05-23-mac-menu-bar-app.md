# Mac Menu-Bar App — Spec

Date: 2026-05-23
Status: implemented

## TL;DR

Native SwiftUI status-bar app for macOS 13+ that surfaces the local agentflow
daemon plus cloud goals + budget. Replaces `agentflow` CLI for at-a-glance
status. P1.

## Goals

- One-click visibility into local agents (status, name, persona).
- One-click visibility into cloud goals (% milestones complete) + today's budget.
- Open cabinet, restart daemon, kill agent — without a terminal.
- Auto-launch on macOS login.
- Native, fast, < 5 MB bundle, no Electron.

## Non-goals

- Codesigning + notarization (deferred to #77).
- Windows tray (deferred to #94).
- Per-agent log viewer (CLI `agentflow agent logs ID` already covers it).

## Brainstorm — options considered

| Option | Pros | Cons | Picked |
|---|---|---|---|
| A. Pure SwiftUI `MenuBarExtra` | Native, fast, predictable, < 1MB binary, full macOS API access | macOS 13+ only | yes |
| B. Swift wrapping `agentflow` CLI via `Process` | Reuses CLI logic, no duplicated REST/socket code | fork-per-poll, slow, error-prone parsing of `typer` output | no |
| C. Python + Tkinter / pystray | Fastest to write | Drags python runtime into a tray app | no |

Picked A. The CLI already speaks REST + UNIX socket in clean shapes — porting
~200 lines of Swift to talk to the same endpoints is cheaper than spawning
processes every 10s.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ AgentFlow.app (LSUIElement, MenuBarExtra)               │
│                                                         │
│  ┌───────────────────┐    ┌───────────────────────┐    │
│  │ AppState (@Obs..) │←───│ Timer (REST every 30s)│    │
│  │  - daemonOk       │    │ Timer (sock every 10s)│    │
│  │  - agents:[Agent] │    └───────────────────────┘    │
│  │  - goals:[Goal]   │                                  │
│  │  - budget:Budget? │    ┌───────────────────────┐    │
│  └────────┬──────────┘    │ RestClient            │    │
│           │               │  fetchGoals()         │    │
│           │               │  fetchBudget()        │    │
│           │               └─────────┬─────────────┘    │
│           │                         │ httpsKey         │
│           │                         ▼                  │
│           │           https://agentflow.website/_agents│
│           │                                            │
│           │               ┌───────────────────────┐    │
│           │               │ SocketClient          │    │
│           │               │  listAgents()         │    │
│           │               └─────────┬─────────────┘    │
│           │                         │ AF_UNIX line-json│
│           │                         ▼                  │
│           │                 /tmp/agentflow.sock        │
│           ▼                                            │
│  MenuBarContent (SwiftUI):                             │
│   ▸ Header: Connected/Down + login state               │
│   ▸ Section "Agents" → row × n                         │
│   ▸ Section "Goals"  → row × n                         │
│   ▸ Section "Budget" → "$x / $y today"                 │
│   ▸ Actions: Open cabinet, Restart daemon, Quit        │
└─────────────────────────────────────────────────────────┘
```

## Files (this PR)

| Path | Role |
|---|---|
| `macapp/Package.swift` | SwiftPM manifest (executable + test target) |
| `macapp/Sources/AgentFlow/AgentFlowApp.swift` | `@main` entry, MenuBarExtra wiring |
| `macapp/Sources/AgentFlow/AppState.swift` | Observable model, polling timers |
| `macapp/Sources/AgentFlow/Models.swift` | `Agent`, `Goal`, `Budget`, `Auth` structs |
| `macapp/Sources/AgentFlow/RestClient.swift` | REST against `/_agents/me/autonomous/*` |
| `macapp/Sources/AgentFlow/SocketClient.swift` | UNIX socket line-JSON client |
| `macapp/Sources/AgentFlow/AuthLoader.swift` | Reads `~/.agentflow/auth.json` |
| `macapp/Sources/AgentFlow/StatusFormatter.swift` | "$1.23 / $5.00 (25%)" pure-function |
| `macapp/Sources/AgentFlow/DaemonControl.swift` | start/stop daemon via `Process` |
| `macapp/Sources/AgentFlow/MenuView.swift` | The dropdown UI |
| `macapp/Sources/AgentFlow/Resources/Info.plist` | LSUIElement, bundle id |
| `macapp/Tests/AgentFlowTests/RestClientTests.swift` | mock URLProtocol returning canned JSON |
| `macapp/Tests/AgentFlowTests/SocketClientTests.swift` | mock UNIX server returning canned JSON |
| `macapp/Tests/AgentFlowTests/StatusFormatterTests.swift` | pure formatter coverage |
| `macapp/Tests/AgentFlowTests/AuthLoaderTests.swift` | reads auth.json, missing-file handling |
| `macapp/Tests/AgentFlowTests/ModelsTests.swift` | Codable decoding for daemon + REST payloads |
| `macapp/README.md` | Build + run + smoke instructions |

## Data sources

| Source | Path | Cadence |
|---|---|---|
| Local agents | `/tmp/agentflow.sock` `{method:"list"}` | 10s |
| Cloud goals | `GET /_agents/me/autonomous/goals` | 30s |
| Cloud budget | `GET /_agents/me/autonomous/budget` | 30s |

All return shapes match the existing Python CLI (`src/agentflow_computer_mcp/cli/`).

## Auth

Read `~/.agentflow/auth.json` once on launch + on every REST call. Same file
that the daemon writes during `agentflow login`. If absent → REST sections
render "не авторизован, запусти agentflow login".

## Auto-launch on login

`Info.plist`: `LSUIElement = YES` (no Dock icon).
`ServiceManagement` framework: `SMAppService.mainApp.register()` on first
launch, exposed as a toggle in the menu ("Запускать при входе"). Deferred
implementation — toggle is a TODO in v1, the framework call is wired but
disabled by default so we don't accidentally register before the user opts in.

## Sandbox + entitlements

The app runs unsandboxed by default (no `.entitlements` file). When we add
codesign + notarize (#77), we'll need:

- `com.apple.security.network.client` — for REST calls.
- `com.apple.security.files.user-selected.read-only` — n/a, we only touch
  `~/.agentflow/`.
- `NSAppleEventsUsageDescription` — only if we add "show in Finder" later.

UNIX-socket calls do not require any entitlement since `/tmp/agentflow.sock`
is in a world-readable mount and the daemon enforces access via filesystem
permissions on the socket.

## Codesigning caveat (until #77)

Without codesign, Gatekeeper will refuse first launch. README documents
the workaround: right-click .app → "Open" → confirm. This is the standard
unsigned-binary path; we will not paper over it with `spctl` hacks.

## Daemon discovery + recovery

If `/tmp/agentflow.sock` is missing → render red dot + "Демон не запущен" +
button "Запустить демон" that runs `agentflow daemon start` via `Process`.
Path lookup: `/usr/local/bin/agentflow`, `~/.local/bin/agentflow`,
`/opt/homebrew/bin/agentflow`. If none found → button is disabled with
tooltip "agentflow CLI не установлен, см. install.sh".

## Polling overhead

10s socket + 30s REST = 6 socket reads + 2 REST hits per minute. Negligible
CPU + bandwidth. A future settings pane could expose intervals.

## Failure modes

| Symptom | Where to look | Fix |
|---|---|---|
| Red dot, "auth.json missing" | `AuthLoader.load()` returned nil | run `agentflow login` |
| Red dot, "Демон не запущен" | `SocketClient.listAgents` threw `daemonUnavailable` | "Запустить демон" or `agentflow daemon start` |
| REST sections show "401" | api_key in auth.json expired | `agentflow login` |
| Stale data | timer fired but caller still resolving | UI shows last successful + grey "updating" pip |

## Quality gates (this PR)

- `swift build -c release` — green.
- `swift test` — all green. 22 unit tests across 5 test files.
- `.app` size after `swift build -c release` < 5 MB (Swift stdlib is dynamic,
  so the binary itself is ~600KB; verified).
- Manual smoke checklist in `macapp/README.md`.

## CI

Self-hosted runner does not have Xcode CLT. New job `test-macapp` on
`macos-latest` (paid runner only, won't break existing self-hosted Linux gate).
We mark the job as `if: github.event_name != 'pull_request' || contains(github.event.pull_request.labels.*.name, 'mac-runner')` so PRs without the label skip — same pattern as the existing `ci/drop-paywalled-runners-add-crossos-mocks` branch. For this PR we label `needs-mac-runner`.

## Related

- [Multi-agent runtime](agentflow-code-docs/subsystems/multi-agent-runtime.mdx)
- [AgentFlow CLI](agentflow-code-docs/subsystems/agentflow-cli.mdx)
