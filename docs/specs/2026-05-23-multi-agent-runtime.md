# Multi-agent runtime spec (2026-05-23)

## Brainstorm: three options

### Option A — single asyncio process, N agent slots, one ws connection
- One `python` process holds the existing ws bridge.
- Inside it, `AgentRouter` dispatches incoming `task_dispatch` frames (now tagged with `agent_id`) into a per-slot `asyncio.Queue`.
- Each slot owns its own `playwright.BrowserContext` (shared `Browser`, isolated cookies + storage_state).
- Memory: ~200 MB for the Chromium binary + ~30-60 MB per extra context. 4 slots fit on a 4 GB box.
- Isolation: weak — uncaught exception in slot A can bubble into the shared loop if the consumer task isn't wrapped. Solvable with `try/except` at the consumer boundary.
- Cold start: zero — slots are coroutines, spawn in ms.
- Cookie isolation: enforced by Playwright `browser.new_context()`.

### Option B — multiprocessing, one process per agent + a thin ws fan-out daemon
- A tiny `agentflow-router` process owns the ws connection.
- For each agent, fork a `python -m agentflow_computer_mcp --agent-id X` worker; IPC over UNIX socket.
- Isolation: strongest — a slot crash never touches others.
- Memory: each worker repays the Python interpreter cost (~80 MB) plus its own Chromium (~200 MB) — 4 slots ≈ 1.1 GB minimum.
- Cold start: ~2 s per fork on Mac, much worse on Windows where fork is unavailable.
- Cookie isolation: each worker has its own user-data-dir; trivially correct.

### Option C — hybrid
- Main process owns ws + dispatcher.
- Slots are subprocesses (Playwright per process) reached via a multiplexed UNIX socket.
- Combines A's low ws overhead with B's strong crash isolation.
- Operational cost: subprocess lifecycle (restart-on-crash supervisor, log multiplexing, IPC schema).
- Cold start: same as B.

## Recommendation: Option A for v1

Rationale:
1. Memory budget. Most users are on 8-16 GB laptops; 4 contexts at ~250 MB delta beats 4 forks at ~1 GB delta.
2. Cookie isolation is already covered by Playwright contexts; we don't gain real-world safety from process isolation.
3. Crash blast radius is bounded by wrapping the slot consumer in `try/except` and letting the slot transition to `status="crashed"`. The ws + other slots survive.
4. v1 ships in ~600 LoC; Option B doubles that for IPC plumbing we don't need yet.
5. If a user reports a real crash-bleeds-into-others incident, Option C is a follow-up — the public surface (`AgentSlot`, `AgentRouter`, UNIX control socket) doesn't change.

## Components

| Name | File | Role |
|---|---|---|
| `AgentSlot` | `agents/slot.py` | dataclass holding id/name/persona/scope/browser_context/queue/status/budget_remaining |
| `AgentRouter` | `agents/router.py` | route ws frames by `agent_id` into the matching slot's queue; spawn/stop consumer tasks |
| `BrowserPool` | `agents/pool.py` | lazy launch of shared `Browser`, hand out fresh `BrowserContext`; hard cap 4 browsers / 8 contexts |
| `BudgetSlice` | `agents/budget.py` | per-slot USD remaining; deducts on every LLM call; raises `BudgetExhausted` |
| `AgentSocket` | `agents/socket.py` | UNIX socket / named pipe; line-JSON protocol for tray + CLI |
| `bootstrap()` | `agents/bootstrap.py` | scan `~/.agentflow/agents/*/`, migrate legacy single-agent install, return list of slots |

## Interfaces

### WS protocol additions

Server now tags `task_dispatch` frames with `agent_id`:

```jsonc
{
  "type": "task_dispatch",
  "id": "task-7",
  "agent_id": "trader",        // NEW — slot id; falls back to "default" if missing
  "task": "...",
  "scope": { ... }
}
```

`hello` outbound is unchanged for v1 (no slot enumeration); a follow-up PR on `agentflow-agents` will add `agent_ids: []` echo. For backward compat the daemon also writes `default_agent_id` into `auth.json` so the server can default to the legacy slot until it learns slot ids.

### Control socket protocol

```jsonc
// request
{"method": "list"}
{"method": "logs", "id": "trader", "n": 100}
{"method": "pause", "id": "trader"}
{"method": "resume", "id": "trader"}
{"method": "create", "name": "trader", "persona": "trade", "scope_path": "~/scopes/trader.toml"}

// response
{"ok": true, "result": [...]}
{"ok": false, "error": "no such slot"}
```

Socket path: `/tmp/agentflow.sock` on POSIX, named pipe `\\.\pipe\agentflow` on Windows.

## Data flow

```
       ┌──────────────────────────────────────────────────┐
       │              agentflow-desktop process            │
       │                                                   │
ws ───► │  WSClient ──► AgentRouter ──► slot[id].queue      │
       │                                  │                │
       │                                  ▼                │
       │                       consumer-task(slot)         │
       │                                  │                │
       │   ┌──────────────────────────────┘                │
       │   ▼                                               │
       │  driver.run_task(state, executor, ..., context=…) │
       │     │                                             │
       │     └─► Playwright BrowserContext (per slot)      │
       │                                                   │
       │  AgentSocket ──► list/pause/resume/create         │
       └──────────────────────────────────────────────────┘
```

## DB / Env / API

- Per-slot dir: `~/.agentflow/agents/<id>/`
  - `scope.toml`        — same shape as legacy `computer-scope.toml`
  - `memory.db`         — sqlite, schema cloned from `autonomous/schema.py`
  - `logs/<YYYY-MM-DD>.jsonl`
  - `persona.txt`       — free-form persona prompt fragment
- Env:
  - `AGENTFLOW_MULTI_AGENT=1` opt-in for v1 (defaults off; single-slot daemon stays the default until UX matures)
  - `AGENTFLOW_AGENT_SOCKET` override socket path
- Routes: none added in this PR (server work is separate).

## Edge cases

| Case | Handling |
|---|---|
| `task_dispatch` arrives for unknown `agent_id` | log warning, fall back to `default` slot |
| Slot consumer raises | wrap in try/except, set `status=crashed`, emit one-line stderr; ws + other slots untouched |
| BrowserPool above cap | reject `create`, return `{"ok": false, "error": "pool_full"}` on the socket |
| Two slots want the same cookie domain | each has own context — no collision possible |
| Budget exhausted mid-task | consumer catches `BudgetExhausted`, marks slot `paused`, posts a `task_complete` with `error="budget_exhausted"` |
| Legacy install (`~/.agentflow/auth.json` + `computer-scope.toml`) | first boot moves them under `agents/default/`, leaves a `.migrated` marker |
| Two daemons started | second one fails on `bind()` of the UNIX socket; exits cleanly |
| Crash mid-context | `BrowserPool` reaps zombie contexts at slot teardown; no orphaned chromium PIDs |

## Failure modes (will land in RAG)

| Symptom | Greppable log | Where to fix |
|---|---|---|
| Slot stays `crashed` after exception | `[agent-router] slot %s crashed` | `agents/router.py:_consume` |
| Browser pool refuses new slot | `[browser-pool] cap reached` | `agents/pool.py:acquire` |
| Socket bind fails on second daemon | `[agent-socket] bind failed` | `agents/socket.py:serve` |
| Legacy migration skipped | `[agent-bootstrap] legacy migrate` | `agents/bootstrap.py:migrate_legacy` |

## Related

- `agentflow-code-docs/subsystems/multi-agent-runtime.mdx` (new in this PR)
- Existing single-agent path: `desktop_cli.py:cmd_run`
