"""Child-process entry for the multi-agent self-test harness.

Spawned by `tests/integration/conftest.py` as `python -m
tests.integration.runtime_harness`. Boots the same
`agents._runtime.maybe_start_runtime` glue the production daemon uses,
under an isolated `HOME` + socket path so the test can hammer it
without colliding with the user's real `~/.agentflow/`.

Inputs (env):
  HOME                       — temp dir; AGENTFLOW_DIR lives under it
  AGENTFLOW_MULTI_AGENT      — must be "1" for slots to register
  AGENTFLOW_AGENT_SOCKET     — full path to the UNIX socket
  AGENTFLOW_HARNESS_READY    — optional file path; harness touches it
                               once the socket is live

Output: blocks on SIGTERM, then tears the router + socket down.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

# Import lazily so a missing dep on the cli/macapp paths cannot block boot.
from agentflow_computer_mcp.agents import _runtime as agents_runtime


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def main() -> int:
    _setup_logging()
    log = logging.getLogger("runtime_harness")

    os.environ.setdefault("AGENTFLOW_MULTI_AGENT", "1")

    # The runtime reads ~/.agentflow/ from config.py. We rely on the
    # parent setting HOME to a temp dir so AGENTFLOW_DIR follows.
    home = Path(os.environ.get("HOME", "/tmp"))
    agentflow_dir = home / ".agentflow"
    agents_dir = agentflow_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    # Guarantee a `default` slot dir so discover_slots returns at least one
    # entry. Without this the runtime treats `slots == []` as "not ready"
    # and returns None.
    (agents_dir / "default").mkdir(parents=True, exist_ok=True)

    handle = agents_runtime.maybe_start_runtime()
    if handle is None:
        log.error("maybe_start_runtime returned None; check env")
        return 1

    socket_path = os.environ.get("AGENTFLOW_AGENT_SOCKET", "/tmp/agentflow.sock")
    # Wait until the socket file is present before we signal the parent.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if Path(socket_path).exists():
            break
        time.sleep(0.05)

    ready_file = os.environ.get("AGENTFLOW_HARNESS_READY")
    if ready_file:
        try:
            Path(ready_file).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write ready file %s: %s", ready_file, exc)

    log.info("harness ready: socket=%s pid=%s", socket_path, os.getpid())

    stop = False

    def _on_signal(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        while not stop:
            time.sleep(0.1)
    finally:
        log.info("shutting down")
        handle.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
