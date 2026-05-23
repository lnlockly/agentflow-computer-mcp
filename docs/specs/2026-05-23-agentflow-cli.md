# agentflow CLI — Spec

Date: 2026-05-23
Status: implementing

## TL;DR
Ship a Typer-based `agentflow` CLI bundled with `agentflow-computer-mcp`. The CLI talks to the local daemon over the existing UNIX socket (`agents/socket.py`) for per-machine agent ops, and to `agentflow.website/_agents/me/...` REST for cross-machine goals, budget, and memory. One binary, three platforms (Windows: agent subcommands gated until #94).

## Boundary (Option C — hybrid)
- Local UNIX socket (`/tmp/agentflow.sock`) → `agent list/create/pause/resume/kill/logs`, `daemon status/start/stop`.
- Server REST (`https://agentflow.website/_agents`) with `x-api-key` → `login/whoami/goal*/budget/memory*`.

## Surface

| Command | Transport | Notes |
|---|---|---|
| `agentflow login [--api-key KEY]` | local fs | writes `~/.agentflow/auth.json` mode 0600, masks key in echo |
| `agentflow whoami` | REST GET `/me` | prints user + connected devices, masks key prefix |
| `agentflow daemon status` | local | checks PID file + socket reachability |
| `agentflow daemon start` | local | `subprocess.Popen` of `agentflow-desktop`, detach |
| `agentflow daemon stop` | local | reads PID file, SIGTERM |
| `agentflow agent list [--remote]` | socket / REST | tabular: id, name, persona, status |
| `agentflow agent create NAME --persona P [--scope F]` | socket | `create` method |
| `agentflow agent pause ID` | socket | |
| `agentflow agent resume ID` | socket | |
| `agentflow agent kill ID` | socket | (alias of pause + remove for v1) |
| `agentflow agent logs ID [--tail N] [--follow]` | socket | `logs` method |
| `agentflow goal list` | REST GET `/me/autonomous/goals` | |
| `agentflow goal create TITLE [--metric M] [--target N] [--deadline ISO]` | REST POST | |
| `agentflow goal show ID` | REST GET | milestones + today's plan |
| `agentflow goal pause/resume ID` | REST POST | |
| `agentflow budget` | REST GET `/me/autonomous/budget` | `$X / $Y (Z%)` |
| `agentflow memory recall QUERY` | REST GET `/me/memory/search` | |
| `agentflow memory record-skill NAME --steps ...` | REST POST `/me/memory/skills` | idempotent |

## Files

- `src/agentflow_computer_mcp/cli/main.py` — Typer app, top-level subcommand wiring
- `src/agentflow_computer_mcp/cli/auth_cli.py` — login, whoami
- `src/agentflow_computer_mcp/cli/daemon.py` — daemon status/start/stop
- `src/agentflow_computer_mcp/cli/agent.py` — agent CRUD via socket
- `src/agentflow_computer_mcp/cli/goal.py` — goal REST
- `src/agentflow_computer_mcp/cli/budget.py` — budget
- `src/agentflow_computer_mcp/cli/memory.py` — memory recall + skill record
- `src/agentflow_computer_mcp/cli/socket_client.py` — thin async client for `agents/socket.py`
- `src/agentflow_computer_mcp/cli/rest_client.py` — httpx wrapper, `x-api-key` injection
- `src/agentflow_computer_mcp/cli/format.py` — table + key-mask helpers
- `pyproject.toml` entry point: `agentflow = "agentflow_computer_mcp.cli.main:app"`

## Auth

- Read order: `--api-key` flag → `AGENTFLOW_API_KEY` env → `~/.agentflow/auth.json`.
- `whoami` masks: `af_live_xxxxxxxx...` (first 8 chars + ...).
- `login` writes mode 0600 via existing `auth.save_auth()`.

## Windows caveat
- `sys.platform == 'win32'` → `agent` and `daemon` subcommands print clear message «требует macOS/Linux пока #94 не выкатился», exit 2.
- `login`, `whoami`, `goal*`, `budget`, `memory*` work on Windows (REST only).

## Failure modes

| Symptom | Cause | UX |
|---|---|---|
| `daemon не запущен` | socket missing or connect fails | print «запусти `agentflow daemon start`», exit 3 |
| `not authenticated` | no api_key | print «запусти `agentflow login`», exit 4 |
| `network error` | httpx ConnectError | print short message, exit 5 |
| `server 4xx/5xx` | bad request | print status + body excerpt, exit 6 |

## Tests
- `tests/cli/test_login.py` — file written 0600, masked echo
- `tests/cli/test_agent_list.py` — mock socket, table output
- `tests/cli/test_goal_create.py` — mock httpx, assert POST body
- `tests/cli/test_budget.py` — mock REST, formatted output
- `tests/cli/test_whoami.py` — mock REST, masked key
- `tests/cli/test_daemon_status.py` — pid file + socket probe

## Out of scope
- Daemon foreground mode (use existing `agentflow-desktop` instead).
- Windows agent subcommands (#94).
- Interactive REPL.
