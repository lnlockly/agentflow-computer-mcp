# Plan — Multi-agent self-test harness

Each step is 2-5 min, TDD-first. Run from `/tmp/af-selftest/` with
`source .venv/bin/activate`.

## Step 0 — branch + dirs
- `git checkout -b feat/multi-agent-selftest-harness` (already on it)
- `mkdir -p tests/integration`
- `touch tests/integration/__init__.py`

## Step 1 — failing: `test_daemon_starts_and_socket_appears`
- File `tests/integration/test_multi_agent_lifecycle.py`.
- Test asserts `daemon.socket_path.exists()` inside fixture yield.
- Run: red (no fixture, no harness, no runner).

## Step 2 — harness runner + fixture
- `tests/integration/runtime_harness.py` — `python -m` entry that
  reads `AGENTFLOW_AGENT_SOCKET` + `AGENTFLOW_HOME` + sets
  `AGENTFLOW_MULTI_AGENT=1`, then calls
  `agents._runtime.maybe_start_runtime()` and blocks on SIGTERM.
- `tests/integration/conftest.py` — `daemon` fixture: spawn subprocess
  with isolated env, poll for socket up to 10s, yield handle, kill on
  teardown.
- Run: Step 1 green.

## Step 3 — failing: `test_spawn_two_agents_lists_both`
- `daemon.spawn_agent("trader")` + `daemon.spawn_agent("writer")` →
  `daemon.list_agents()` returns 3 (default + 2).
- Run: red — no `spawn_agent` helper yet.

## Step 4 — extend handle with `spawn_agent` + `list_agents`
- Both call into `cli.socket_client.call(...)` against the fixture's
  socket path.
- Run: Step 3 green.

## Step 5 — failing: `test_pause_resume_round_trip`
- `pause` → status=paused, `resume` → status=idle.
- Run: red until we add `pause_agent`/`resume_agent` helpers.

## Step 6 — extend handle, green
- Add `pause_agent` + `resume_agent` thin wrappers.
- Run: green.

## Step 7 — failing: `test_kill_one_other_survives`
- Spawn two agents, mark one slot "crashed" via the in-process router
  (proxy: call `pause` to take it out of the consumer rotation), assert
  the other still answers `list` queries.
- Run: red until harness exposes a way to terminate one slot's consumer
  without dropping the whole socket.

## Step 8 — `pause` is the kill-proxy
- In v1, slots can't be hard-killed (the router owns the loop); pause
  represents "this agent is offline" in a way other agents still see.
- Run: Step 7 green.

## Step 9 — failing: `test_scope_isolation_via_create`
- Spawn agent A with a scope.toml that allow_paths=["/tmp/a"], spawn B
  with allow_paths=["/tmp/b"]. Assert the on-disk `scope.toml`s differ
  per slot.
- Run: red until we copy scope content into the slot dir via `create`.

## Step 10 — green via existing `create` semantics
- The `create` method on the daemon already copies `scope_path` into
  the slot's `scope.toml` (see `bootstrap.create_slot_dir`). Verify
  the harness passes it through.
- Run: green.

## Step 11 — failing: `test_socket_env_override`
- Spawn daemon with `AGENTFLOW_AGENT_SOCKET=/tmp/af-custom-<uuid>.sock`,
  assert the socket appears at that exact path (not the default).
- Run: red if the harness ignores the env var.

## Step 12 — pass via existing `_runtime` glue
- `_runtime.py:92` already reads `AGENTFLOW_AGENT_SOCKET`. Verify
  end-to-end.
- Run: green.

## Step 13 — lint + ruff clean
- `ruff check tests/integration/`
- Fix anything that's not a banned pattern.

## Step 14 — wire CI job
- Add `multi-agent-selftest` to `.github/workflows/ci.yml`,
  `runs-on: self-hosted`, depends on `test-linux`.
- Single line: `xvfb-run -a pytest tests/integration/test_multi_agent_lifecycle.py -x -v --timeout=120`.

## Step 15 — RAG page
- `agentflow-code-docs/subsystems/multi-agent-selftest.mdx` with
  files, data flow, failure modes, how-to-extend.

## Step 16 — commit + PR
- One commit. `feat(test): multi-agent e2e self-test harness`.
- Stop-slop check: no `really/simply/deeply/just/actually/literally`,
  no em-dash dividers, no antitheses.
- `gh pr create`, link to spec + RAG.
