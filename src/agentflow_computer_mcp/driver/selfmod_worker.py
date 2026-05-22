"""Background worker that turns selfmod queue entries into real PRs.

For each ``queued`` entry, the worker:

1. Builds a brief from ``reason`` + ``suggested_change``.
2. Spawns ``claude -p <prompt>`` headless inside the repo working tree.
3. Parses its stdout for ``PR: <url>`` (success) or ``REJECT: <reason>``.
4. Inspects the resulting branch's diff against ``origin/main`` and refuses
   to merge if a forbidden path was modified.
5. Optionally runs ``gh pr merge --squash --admin`` then
   ``pip install --upgrade .`` based on env flags.

The worker is safety-first: defaults assume ``--dry-run`` style behaviour
where no PR is auto-merged and no package is auto-upgraded.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from . import selfmod

log = logging.getLogger(__name__)

# Paths the worker will refuse to merge if touched by the spawned Claude
# session. Substring matching against `git diff --name-only`.
FORBIDDEN_PATHS: tuple[str, ...] = (
    ".github/workflows/",
    "src/agentflow_computer_mcp/auth.py",
    "src/agentflow_computer_mcp/config.py",
    "src/agentflow_computer_mcp/driver/selfmod.py",
    "src/agentflow_computer_mcp/driver/selfmod_worker.py",
)

PR_URL_RE = re.compile(r"PR:\s*(https?://\S+)")
REJECT_RE = re.compile(r"REJECT:\s*(.+)", re.IGNORECASE)

DEFAULT_TIMEOUT_SECONDS = 900
POLL_INTERVAL_SECONDS = 5

ENV_AUTOMERGE = "SELFMOD_AUTOMERGE"
ENV_AUTOAPPLY = "SELFMOD_AUTOAPPLY"
ENV_REPO_PATH = "SELFMOD_REPO_PATH"
ENV_CLAUDE_BIN = "SELFMOD_CLAUDE_BIN"


def repo_path() -> Path:
    """Resolve the repo working copy the worker drives.

    Prefers ``SELFMOD_REPO_PATH``, falls back to walking up from this file
    to the package root.
    """
    override = os.environ.get(ENV_REPO_PATH)
    if override:
        return Path(override).expanduser().resolve()
    # this file lives at .../src/agentflow_computer_mcp/driver/selfmod_worker.py
    return Path(__file__).resolve().parents[3]


def claude_binary() -> str:
    return os.environ.get(ENV_CLAUDE_BIN) or "claude"


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_prompt(request: dict[str, object]) -> str:
    reason = str(request.get("reason", "")).strip()
    suggested = str(request.get("suggested_change", "")).strip()
    urgency = str(request.get("urgency", "normal"))
    return (
        "You are a code agent on the agentflow-computer-mcp repo. Implement the change below.\n\n"
        f"REASON: {reason}\n"
        f"SUGGESTED CHANGE: {suggested}\n"
        f"URGENCY: {urgency}\n\n"
        "Rules:\n"
        " - Work on a new branch named `auto/selfmod-<short-id>`.\n"
        " - Do not touch .github/workflows/, auth.py, config.py, selfmod.py, selfmod_worker.py.\n"
        " - Run ruff and pytest locally. Both must pass.\n"
        " - When done, push and open a PR with `gh pr create --fill`.\n"
        " - End your output with exactly one line: `PR: <url>` on success, or `REJECT: <reason>` if unsafe.\n"
    )


def _run_git(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def detect_forbidden_in_diff(diff_names: list[str]) -> list[str]:
    """Return the list of forbidden paths that appeared in the diff."""
    hits: list[str] = []
    for name in diff_names:
        for bad in FORBIDDEN_PATHS:
            if bad in name:
                hits.append(name)
                break
    return hits


def changed_files_against_main(cwd: Path) -> list[str]:
    rc, out = _run_git(["diff", "--name-only", "origin/main...HEAD"], cwd)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def parse_claude_output(stdout: str) -> tuple[str | None, str | None]:
    """Return (pr_url, reject_reason). Both None means the run is malformed."""
    pr_match = PR_URL_RE.search(stdout)
    if pr_match:
        return pr_match.group(1).strip(), None
    rej_match = REJECT_RE.search(stdout)
    if rej_match:
        return None, rej_match.group(1).strip()
    return None, None


def _spawn_claude(
    prompt: str,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        claude_binary(),
        "-p",
        prompt,
        "--add-dir",
        str(cwd),
        "--allowedTools",
        "Read,Edit,Write,Bash(git:*),Bash(gh:*),Bash(pytest:*),Bash(ruff:*)",
    ]
    log.info("spawning claude (%ds budget)", timeout)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _merge_pr(pr_url: str, cwd: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["gh", "pr", "merge", pr_url, "--squash", "--admin"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def _pip_self_upgrade(cwd: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "."],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def process_request(
    request: dict[str, object],
    *,
    automerge: bool,
    autoapply: bool,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    spawn: Callable[[str, Path, int], subprocess.CompletedProcess[str]] | None = None,
    merge: Callable[[str, Path], tuple[bool, str]] | None = None,
    apply: Callable[[Path], tuple[bool, str]] | None = None,
) -> dict[str, object]:
    """Drive one request end to end.

    The ``spawn``/``merge``/``apply`` overrides exist so tests can inject
    deterministic fakes without actually shelling out.
    """
    rid = str(request["request_id"])
    cwd = repo_path()
    if not (cwd / ".git").exists():
        selfmod.update_status(rid, "failed", error=f"no git repo at {cwd}")
        return {"status": "failed", "error": "no git repo"}

    if spawn is None and shutil.which(claude_binary()) is None:
        selfmod.update_status(rid, "failed", error=f"`{claude_binary()}` binary not on PATH")
        return {"status": "failed", "error": "claude binary missing"}

    prompt = build_prompt(request)
    spawn_fn = spawn or _spawn_claude

    try:
        proc = spawn_fn(prompt, cwd, timeout)
    except subprocess.TimeoutExpired:
        selfmod.update_status(rid, "failed", error=f"claude timed out after {timeout}s")
        return {"status": "failed", "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        selfmod.update_status(rid, "failed", error=f"spawn error: {exc}")
        return {"status": "failed", "error": str(exc)}

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    pr_url, reject_reason = parse_claude_output(combined)

    if reject_reason is not None:
        selfmod.update_status(rid, "rejected", error=reject_reason)
        return {"status": "rejected", "reason": reject_reason}

    if pr_url is None or proc.returncode != 0:
        snippet = combined.strip()[-400:]
        selfmod.update_status(rid, "failed", error=f"no PR url (rc={proc.returncode}): {snippet}")
        return {"status": "failed", "error": "no PR url"}

    diff_names = changed_files_against_main(cwd)
    hits = detect_forbidden_in_diff(diff_names)
    if hits:
        selfmod.update_status(
            rid,
            "rejected",
            pr_url=pr_url,
            error=f"forbidden paths touched: {', '.join(hits)}",
        )
        return {"status": "rejected", "reason": "forbidden_paths", "paths": hits}

    selfmod.update_status(rid, "pr_opened", pr_url=pr_url)

    if not automerge:
        return {"status": "pr_opened", "pr_url": pr_url}

    merge_fn = merge or _merge_pr
    ok, merge_out = merge_fn(pr_url, cwd)
    if not ok:
        selfmod.update_status(rid, "failed", pr_url=pr_url, error=f"merge failed: {merge_out[-300:]}")
        return {"status": "failed", "error": "merge failed", "pr_url": pr_url}

    selfmod.update_status(rid, "merged", pr_url=pr_url)

    if not autoapply:
        return {"status": "merged", "pr_url": pr_url}

    apply_fn = apply or _pip_self_upgrade
    ok, apply_out = apply_fn(cwd)
    if not ok:
        log.warning("self-upgrade failed: %s", apply_out[-300:])
    return {"status": "merged", "pr_url": pr_url, "upgraded": ok}


class SelfmodWorker:
    """Single-threaded poller that drains the queue."""

    def __init__(
        self,
        *,
        automerge: bool | None = None,
        autoapply: bool | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.automerge = env_flag(ENV_AUTOMERGE, False) if automerge is None else automerge
        self.autoapply = env_flag(ENV_AUTOAPPLY, False) if autoapply is None else autoapply
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        t = threading.Thread(target=self._run, name="selfmod-worker", daemon=True)
        t.start()
        self._thread = t
        log.info(
            "selfmod worker started (automerge=%s autoapply=%s)",
            self.automerge,
            self.autoapply,
        )
        return t

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            request = selfmod.pop_next_queued()
            if request is None:
                self._stop.wait(self.poll_interval)
                continue
            try:
                process_request(
                    request,
                    automerge=self.automerge,
                    autoapply=self.autoapply,
                    timeout=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("selfmod worker crash on %s", request.get("request_id"))
                selfmod.update_status(
                    str(request["request_id"]),
                    "failed",
                    error=f"worker crash: {exc}",
                )
