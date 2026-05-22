# Self-modification loop

The AgentFlow Desktop daemon can request its own code to change. When the
driver's LLM hits a wall — a missing tool, a broken parser, a slow capture
loop — it calls `selfmod_request_change(reason, suggested_change)`. The
request lands in a JSONL queue. A background worker picks it up, spawns
`claude -p` against the `agentflow-computer-mcp` working copy, parses the
result, and (optionally) merges + auto-upgrades.

The defaults are safe. Auto-merge and auto-upgrade are off until a human
flips them on.

## Surface

### Tools exposed to the LLM

| Tool | Purpose |
|---|---|
| `selfmod_request_change(reason, suggested_change, urgency?)` | Queue a change request. Returns `{request_id, queued, status}`. |
| `selfmod_list_recent(limit?)` | Inspect recent requests + statuses. |

`urgency` is `low | normal | high`. Default `normal`. The worker treats all
three the same today; it's reserved for future scheduling.

### CLI

```
agentflow-desktop selfmod list [--limit N]
agentflow-desktop selfmod retry <request_id>
agentflow-desktop selfmod cancel <request_id>
```

The worker starts automatically as part of `agentflow-desktop run` unless
`--no-selfmod` is passed.

### Run flags

```
agentflow-desktop run \
  [--no-selfmod] \
  [--selfmod-automerge] \
  [--selfmod-autoapply]
```

Each flag can also be set via env: `SELFMOD_AUTOMERGE=1`,
`SELFMOD_AUTOAPPLY=1`. Defaults: both off.

### Env

| Var | Default | Purpose |
|---|---|---|
| `AGENTFLOW_DESKTOP_HOME` | `~/.agentflow-desktop` | Where the queue file lives. |
| `SELFMOD_AUTOMERGE` | `0` | If `1`, runs `gh pr merge --squash --admin` after a PR is opened. |
| `SELFMOD_AUTOAPPLY` | `0` | If `1` (and a merge happened), runs `pip install --upgrade .`. |
| `SELFMOD_REPO_PATH` | derived from package location | Working copy the worker drives. |
| `SELFMOD_CLAUDE_BIN` | `claude` | Override the headless code-agent binary. |

## Storage

```
~/.agentflow-desktop/selfmod-queue.jsonl
```

One JSON object per line. Fields:

```
request_id        sm-<12 hex>
reason            free text
suggested_change  free text
urgency           low | normal | high
created_at        unix seconds
status            queued | in_progress | pr_opened | merged
                  | rejected | failed | throttled | cancelled
pr_url            null until the agent opens one
error             populated on rejected/failed/throttled
updated_at        unix seconds, last status change
```

## Threat model

The worker spawns a separate Claude process that can run `Read,Edit,Write`
plus `Bash(git:*) Bash(gh:*) Bash(pytest:*) Bash(ruff:*)`. That's enough to
publish code under your name. The mitigations:

1. **Allowlist over denylist for the subprocess.** Only the four bash
   namespaces above. No `Bash(curl:*)`, no `Bash(rm:*)`, no shell at large.
2. **Forbidden-path gate** after the subprocess returns. If
   `git diff --name-only origin/main...HEAD` touches any of:
   - `.github/workflows/`
   - `src/agentflow_computer_mcp/auth.py`
   - `src/agentflow_computer_mcp/config.py`
   - `src/agentflow_computer_mcp/driver/selfmod.py`
   - `src/agentflow_computer_mcp/driver/selfmod_worker.py`
   the worker marks the request `rejected` and refuses to merge — even with
   `SELFMOD_AUTOMERGE=1`. The PR stays open on GitHub for human review.
3. **Rate limit.** One accepted request per 15 minutes. The remaining
   requests are stored with `status=throttled` so they show up in
   `selfmod list` but never feed the worker.
4. **Safe defaults.** A daemon started with no env / no flags will open
   PRs at most — never merge, never upgrade itself.
5. **Append-only audit log.** The queue is JSONL. Status changes rewrite
   the file via a `.tmp` + atomic rename, but nothing is deleted; cancel
   just flips the status.

## What "self-modification" means in practice

The most useful changes the daemon can request itself:

- Add a tool the LLM lacked (e.g. `mouse_drag`, `pdf_extract_text`).
- Improve a parser that misread terminal output.
- Tune `FORBIDDEN_PATHS` of unrelated subsystems.
- Add a regression test for a flaky path the daemon noticed.

Things it should not request and which `FORBIDDEN_PATHS` will block:

- Editing CI to weaken the gate.
- Editing auth / config to leak the API key.
- Editing the selfmod files themselves to disarm the safety rails.

## Example end-to-end

```python
# inside the LLM tool loop
selfmod_request_change(
    reason="iTerm capture sometimes truncates at 2500 chars on 4k displays",
    suggested_change=(
        "in src/agentflow_computer_mcp/driver/desktop_tools.py "
        "lift the implicit 2500 char cap to 8000. "
        "Add a test in tests/test_desktop_tools.py that asserts >2500."
    ),
    urgency="normal",
)
```

The worker:

1. Marks the row `in_progress`.
2. Spawns `claude -p "<prompt>" --add-dir <repo> --allowedTools …`.
3. Reads stdout. Expects `PR: https://github.com/…/pull/N`.
4. Diffs the branch against `origin/main`. If `desktop_tools.py` is the
   only changed code file plus a new test — no forbidden paths — sets
   `status=pr_opened` and stops (default) or merges + `pip install
   --upgrade .` (when both flags are on).
