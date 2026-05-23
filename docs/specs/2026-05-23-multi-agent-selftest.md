# Multi-agent self-test harness (#89)

## TL;DR
End-to-end pytest harness that boots the multi-agent runtime in a child
process, talks to it over a UNIX socket, and asserts the lifecycle
guarantees the v1 runtime is built on. Catches regressions in router /
socket / scope drift before they reach a release.

## Goals
1. One command (`pytest tests/integration/test_multi_agent_lifecycle.py`)
   spins a clean daemon, runs the suite, tears down.
2. Each test gets an isolated socket path, isolated `AGENTFLOW_HOME`,
   and the daemon child is killed in the fixture `finally`.
3. CI step is wired into the existing self-hosted Linux job in
   `.github/workflows/ci.yml` (the only runner with billing).
4. Read-only against `src/agentflow_computer_mcp/agents/`, `cli/`,
   `macapp/`, `winapp/`. The harness must drive the system-under-test,
   not modify it.

## Non-goals
- Real browser. The runtime's handler is mocked in-harness.
- Network. The harness never opens a WS connection to prod.
- Windows. POSIX-only socket; Windows path is parked behind #94.

## Options considered
- **A. docker-compose** — spin the daemon in a container, exec via
  socket bind-mount. Heavy; the self-hosted runner already runs Linux.
- **B. pytest + subprocess + UNIX socket** ← chosen. Subprocess + socket
  is exactly the integration boundary we want to assert.
- **C. CI-only matrix** — wraps B; chosen as the deployment shape.

## Architecture

```
pytest test                       child python -m tests.integration.runtime_harness
  │                                  │
  │ spawn subprocess ───────────────►│
  │  (env: AGENTFLOW_HOME=tmp,        │ AgentRouter + AgentSocket
  │       AGENTFLOW_AGENT_SOCKET=…,    │ in one asyncio loop
  │       AGENTFLOW_MULTI_AGENT=1)     │
  │                                  │
  │   send line-JSON ──── socket ────►│ list / create / pause / resume
  │   ◄──── line-JSON ────────────────┤
  │                                  │
  │ SIGTERM ────────────────────────►│ socket.stop() + router.stop()
```

The child process is the *runtime harness*, not the full
`agentflow-desktop run` daemon. The full daemon also boots capture
loops, an HTTP viewer, and a WS bridge — all of which require an API
key, a screen, and external network. Calling `subprocess` on the full
daemon would either hang or fail at import time in CI. The runtime
harness imports the same `agents._runtime.maybe_start_runtime` glue
the production daemon imports, so we still exercise the boot path
that ships.

## Test file layout
```
tests/integration/
├── __init__.py
├── conftest.py             # daemon fixture
├── runtime_harness.py      # child-process entry point
└── test_multi_agent_lifecycle.py
```

## Daemon fixture API
```python
def test_dispatch_routes_by_agent_id(daemon: DaemonHandle) -> None:
    daemon.spawn_agent("trader", persona="trade safely")
    daemon.spawn_agent("writer", persona="write blog posts")
    agents = daemon.list_agents()
    assert {a["id"] for a in agents} == {"default", "trader", "writer"}
```

`DaemonHandle` exposes:
- `spawn_agent(name, persona="", scope_path=None) -> dict`
- `list_agents() -> list[dict]`
- `pause_agent(id) -> dict`
- `resume_agent(id) -> dict`
- `socket_path: Path`
- `agentflow_home: Path`
- `kill(signal.SIGTERM)` / `kill_pid(pid)` for "kill one, others survive"

## Failure modes caught
| Symptom | What this catches |
|---|---|
| Daemon binds the wrong socket path | env override is ignored |
| `discover_slots` skips multi-agent dirs | env flag flipped silently |
| Router loses tasks during a slot crash | re-introduces a regression of router._consume |
| Socket leaks across runs | stale `/tmp/agentflow.sock` blocks next boot |
| Scope file isolation | one agent's scope reads another's allow_paths |

## Critique
- **Socket race** — child takes ~150ms to bind on a cold Mac. Use
  exponential backoff up to 10s + a final `assert path.exists()`.
- **Crash cleanup** — `yield`-style pytest fixture wraps the kill in
  `finally`. If the test process itself is `kill -9`'d, the cron in
  the runner janitors `/tmp/af-*.sock`.
- **Parallel runs** — each test gets a random `tmp_path` + a random
  socket name. Two pytest workers on the same machine never collide.
- **Self-hosted state** — fixture nukes `AGENTFLOW_HOME` (its own temp
  dir) on teardown. The real `~/.agentflow/` is never touched.

## CI integration
Add a job `multi-agent-selftest` to `.github/workflows/ci.yml` that
runs on `self-hosted` after `test-linux` succeeds. Calls:

```
xvfb-run -a pytest tests/integration/test_multi_agent_lifecycle.py -x -v --timeout=120
```

The job depends on `test-linux` so unit-test regressions short-circuit
the slower e2e step.

## Verification
- Local: `pytest tests/integration/test_multi_agent_lifecycle.py -x -v`
  → all green, runtime under 30s.
- Push branch → observe GitHub Actions `multi-agent-selftest` job
  passes on the self-hosted runner.
- RAG page `agentflow-code-docs/subsystems/multi-agent-selftest.mdx`
  exists and references the same test paths + failure modes.
