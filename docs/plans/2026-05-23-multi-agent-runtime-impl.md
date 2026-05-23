# Multi-agent runtime — implementation plan (TDD)

Each step has a failing test first. Step N+1 starts after step N is green.

## Step 1 — AgentSlot dataclass
**Test:** `tests/test_multi_agent_slot.py::test_slot_defaults` asserts `AgentSlot(id="trader")` carries `status="idle"`, an `asyncio.Queue`, `budget_remaining_usd=2.0`.
**Impl:** `src/agentflow_computer_mcp/agents/slot.py` — dataclass with field factories.
**Verify:** `pytest -k slot_defaults`.

## Step 2 — BrowserPool gives two distinct contexts
**Test:** `tests/test_multi_agent_pool.py::test_two_slots_two_contexts` — playwright mocked; `await pool.acquire("a")` and `await pool.acquire("b")` return two `MagicMock` contexts with different ids.
**Impl:** `agents/pool.py` — lazy `playwright.chromium.launch()`, `browser.new_context()` per call, dict `slot_id → context`, hard cap.
**Verify:** `pytest -k two_contexts`.

## Step 3 — Pool cap rejects beyond limit
**Test:** `test_pool_cap_reached` — acquire 8 contexts, 9th raises `PoolFull`.
**Impl:** raise after dict reaches `max_contexts`.

## Step 4 — Cookie isolation between slots
**Test:** `test_cookie_isolation` — using mocked context, slot A's `add_cookies` call list is independent of slot B's; assert each context received its own `add_cookies` but never the other's.
**Impl:** already correct by construction; test guards regression.

## Step 5 — BudgetSlice deducts and raises
**Test:** `tests/test_multi_agent_budget.py::test_deduct_then_exhaust` — `BudgetSlice(0.10)`; `deduct(0.04)`; `deduct(0.04)`; third `deduct(0.04)` raises `BudgetExhausted`.
**Impl:** `agents/budget.py` — tiny class, atomic via `asyncio.Lock`.

## Step 6 — Router routes by agent_id
**Test:** `tests/test_multi_agent_router.py::test_dispatch_routes_to_correct_slot` — build router with slots `a`, `b`; call `router.dispatch({"agent_id":"b","id":"t1","task":"…"})`; assert slot `b`'s queue has 1 item, `a`'s has 0.
**Impl:** `agents/router.py::AgentRouter.dispatch`.

## Step 7 — Unknown agent_id falls back to default
**Test:** `test_unknown_agent_falls_back_to_default` — slot `default` exists; dispatch with `agent_id="ghost"`; `default.queue` has it.
**Impl:** `dispatch` matches by id else falls back to `self.slots["default"]`.

## Step 8 — Slot consumer survives an exception
**Test:** `test_consumer_survives_crash` — push two tasks; first handler raises; second still runs; slot status moves through `running → crashed → idle` (recoverable).
**Impl:** wrap handler in `try/except`; set status; continue consuming.

## Step 9 — Control socket lists slots
**Test:** `tests/test_multi_agent_socket.py::test_list_returns_slot_ids` — start socket on tmp path; `nc`-style client sends `{"method":"list"}`; receives `{"ok":true,"result":[{"id":"default", ...}]}`.
**Impl:** `agents/socket.py` — asyncio start_unix_server (POSIX); Windows path handled separately (skip the test on win32).

## Step 10 — Socket create makes a new slot dir
**Test:** `test_create_persists_dir` — POSIX-only; sends `create` with name/persona/scope_path; assert `~/.agentflow/agents/<id>/scope.toml` exists; slot in router.

## Step 11 — Legacy migration moves auth+scope into default/
**Test:** `tests/test_multi_agent_bootstrap.py::test_legacy_migration` — tmp `~/.agentflow` with `auth.json` and `computer-scope.toml`; run `bootstrap`; assert files moved into `agents/default/` and `.migrated` marker exists.
**Impl:** `agents/bootstrap.py::migrate_legacy`.

## Step 12 — Backwards-compat: no `AGENTFLOW_MULTI_AGENT` env → single slot
**Test:** `test_single_slot_when_disabled` — env unset; `discover_slots()` returns exactly one slot id `default`.
**Impl:** `bootstrap.discover_slots` checks env.

## Step 13 — End-to-end smoke (no real browser)
**Test:** `tests/test_multi_agent_e2e.py::test_two_slots_two_contexts_two_tasks` — fake playwright + mock LLM; spawn router with two slots; dispatch one task to each; assert both consumers ran with their own context_id; assert no cookie leakage; assert both tasks complete.

## Step 14 — PyInstaller smoke import
Add `installer/smoke.py` imports for `agents.slot`, `agents.router`, `agents.pool`, `agents.socket`, `agents.budget`, `agents.bootstrap`.

## Step 15 — RAG docs
Write `agentflow-code-docs/subsystems/multi-agent-runtime.mdx` (template).
