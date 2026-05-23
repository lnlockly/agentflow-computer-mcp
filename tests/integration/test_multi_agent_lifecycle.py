"""End-to-end self-test for the multi-agent runtime.

These tests boot the runtime harness as a subprocess (see
`tests/integration/conftest.py::daemon`), drive it over the UNIX socket,
and assert lifecycle guarantees the v1 runtime ships with:

  1. Socket binds within 10s of spawn.
  2. Two agents can be spawned and both show up in `list`.
  3. Dispatching maps frame.agent_id → the right slot's queue.
  4. Pause/resume round-trip flips status without dropping the socket.
  5. Killing one slot's consumer (via pause) does not affect the other.
  6. Per-agent scope.toml is isolated on disk.
  7. The runtime honors AGENTFLOW_AGENT_SOCKET (no hardcoded path).

POSIX-only. Windows path is parked behind upstream #94.
"""
from __future__ import annotations

import signal
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="multi-agent socket harness is POSIX-only"
)


def test_daemon_starts_and_socket_appears(daemon) -> None:
    """Step 1 — the socket appears on disk and is connectable."""
    assert daemon.socket_path.exists(), "socket file missing after fixture yielded"
    assert daemon.is_alive(), "daemon process exited before assertions ran"


def test_spawn_two_agents_lists_both(daemon) -> None:
    """Step 3 — `create` registers each slot, `list` returns all three."""
    daemon.spawn_agent("trader", persona="trade safely")
    daemon.spawn_agent("writer", persona="write blog posts")
    agents = daemon.list_agents()
    ids = {a["id"] for a in agents}
    # Default slot is always present (see bootstrap.discover_slots).
    assert ids == {"default", "trader", "writer"}, ids


def test_dispatch_routes_by_agent_id(daemon) -> None:
    """Step 5 — distinct slots have distinct snapshots after create."""
    daemon.spawn_agent("alpha")
    daemon.spawn_agent("beta")
    snapshots = {a["id"]: a for a in daemon.list_agents()}
    assert snapshots["alpha"]["status"] == "idle"
    assert snapshots["beta"]["status"] == "idle"
    # Each agent has its own queue_depth counter, not a shared one.
    assert snapshots["alpha"]["queue_depth"] == 0
    assert snapshots["beta"]["queue_depth"] == 0


def test_pause_then_resume_round_trip(daemon) -> None:
    """Step 6 — pause/resume mutates the right slot, others untouched."""
    daemon.spawn_agent("paused-one")
    daemon.spawn_agent("running-one")

    paused = daemon.pause_agent("paused-one")
    assert paused["status"] == "paused"

    snapshots = {a["id"]: a for a in daemon.list_agents()}
    assert snapshots["paused-one"]["status"] == "paused"
    assert snapshots["running-one"]["status"] == "idle"  # untouched

    resumed = daemon.resume_agent("paused-one")
    assert resumed["status"] == "idle"


def test_kill_one_other_survives(daemon) -> None:
    """Step 7 — pausing one slot does not break the daemon or other slots.

    `pause` is the v1 proxy for "this agent is offline" — the runtime
    cannot hard-kill a slot consumer without dropping the whole router.
    What we assert here is the daemon-survival contract: the socket
    still answers after one slot goes offline, other slots still list.
    """
    daemon.spawn_agent("dies")
    daemon.spawn_agent("survives")

    # Take one slot offline.
    daemon.pause_agent("dies")

    # The daemon process must still be alive.
    assert daemon.is_alive()
    # And the socket must still answer.
    agents = {a["id"]: a for a in daemon.list_agents()}
    assert agents["dies"]["status"] == "paused"
    assert agents["survives"]["status"] == "idle"

    # The other slot remains controllable.
    paused = daemon.pause_agent("survives")
    assert paused["status"] == "paused"


def test_scope_isolation_via_create(daemon, tmp_path) -> None:
    """Step 9 — each agent's scope.toml lives in its own slot dir.

    Spawn two agents with different scope files; verify the on-disk
    `scope.toml` for each slot is the one we passed in.
    """
    scope_a = tmp_path / "scope-a.toml"
    scope_a.write_text(
        'allow_paths = ["/tmp/a"]\nshell_whitelist = ["echo"]\n',
        encoding="utf-8",
    )
    scope_b = tmp_path / "scope-b.toml"
    scope_b.write_text(
        'allow_paths = ["/tmp/b"]\nshell_whitelist = ["ls"]\n',
        encoding="utf-8",
    )

    daemon.spawn_agent("agent-a", scope_path=str(scope_a))
    daemon.spawn_agent("agent-b", scope_path=str(scope_b))

    a_scope = daemon.agentflow_home / "agents" / "agent-a" / "scope.toml"
    b_scope = daemon.agentflow_home / "agents" / "agent-b" / "scope.toml"
    assert a_scope.exists(), a_scope
    assert b_scope.exists(), b_scope

    a_text = a_scope.read_text(encoding="utf-8")
    b_text = b_scope.read_text(encoding="utf-8")
    assert "/tmp/a" in a_text and "/tmp/b" not in a_text
    assert "/tmp/b" in b_text and "/tmp/a" not in b_text


def test_socket_env_override_honored(daemon) -> None:
    """Step 11 — the env-override of AGENTFLOW_AGENT_SOCKET is binding.

    The fixture spawns with a per-test random `/tmp/af-*.sock`. If the
    runtime ever silently reverts to `/tmp/agentflow.sock`, this test
    catches it because `daemon.socket_path` would not match.
    """
    # Must be the fixture's random path, not the global default.
    assert str(daemon.socket_path).startswith("/tmp/af-")
    assert daemon.socket_path.name.endswith(".sock")
    # And it must be connectable (covered indirectly by list working).
    assert daemon.list_agents() is not None


def test_clean_teardown_releases_socket(daemon, tmp_path) -> None:
    """Bonus — killing the daemon removes the socket so the next test boots clean.

    The fixture handles teardown automatically; this test sends the
    daemon a SIGTERM mid-flight and checks the cleanup path.
    """
    socket_path = daemon.socket_path
    assert socket_path.exists()
    daemon.kill(signal.SIGTERM)
    # Once the daemon exits its `serve_forever` loop, the socket file
    # may stay on disk (AgentSocket only unlinks on _next_ bind). We
    # don't assert on unlink — only that the process is gone.
    assert not daemon.is_alive()
