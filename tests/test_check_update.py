"""Unit tests for the on-demand update flow.

Two surfaces are pinned here:

  • :func:`auto_updater.check_and_apply_once` — the synchronous shape
    the WS handler returns to the backend. Every branch of the
    underlying ``check_now`` status enum maps to a stable
    ``{ok, applied, restarting, reason}`` envelope so the cabinet can
    render a toast without parsing free text.

  • :class:`ws_client.WSClient` ``check_update`` → ``check_update_result``
    round-trip. The handler must respond on the WS with the same id
    and the envelope returned by ``check_and_apply_once`` (or a
    crash-safe fallback when the probe itself blows up).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agentflow_computer_mcp import auto_updater
from agentflow_computer_mcp.config import AppConfig, Auth, Scope
from agentflow_computer_mcp.ws_client import WSClient

# ─────────── check_and_apply_once unit shape ───────────


def test_check_and_apply_once_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_check_now(**_: Any) -> dict:
        return {"status": "current", "current": "0.5.0", "latest": "0.5.0", "reason": "0.5.0 is current (latest 0.5.0)"}

    out = auto_updater.check_and_apply_once(_check=fake_check_now)
    assert out["ok"] is True
    assert out["applied"] is False
    assert out["restarting"] is False
    assert out["reason"] == "up_to_date"
    assert out["latest_version"] == "0.5.0"
    assert out["current_version"] == auto_updater.__version__


def test_check_and_apply_once_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_check_now(**_: Any) -> dict:
        return {
            "status": "applied",
            "current": "0.5.0",
            "latest": "0.6.0",
            "reason": "updated 0.5.0 → 0.6.0",
        }

    out = auto_updater.check_and_apply_once(_check=fake_check_now)
    assert out["ok"] is True
    assert out["applied"] is True
    assert out["restarting"] is True
    assert out["reason"] == "applied"
    assert out["latest_version"] == "0.6.0"


def test_check_and_apply_once_systemexit_means_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows path raises SystemExit after spawning update.bat — we
    must report applied/restarting rather than crash the WS handler."""

    def fake_check_now(**_: Any) -> dict:
        raise SystemExit(0)

    out = auto_updater.check_and_apply_once(_check=fake_check_now)
    assert out["ok"] is True
    assert out["applied"] is True
    assert out["restarting"] is True
    assert out["reason"] == "applied"


def test_check_and_apply_once_running_from_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_check_now(**_: Any) -> dict:
        return {"status": "skipped", "current": "0.5.0", "latest": None, "reason": "running from source"}

    out = auto_updater.check_and_apply_once(_check=fake_check_now)
    assert out["ok"] is True
    assert out["applied"] is False
    assert out["restarting"] is False
    assert out["reason"] == "platform_unsupported"


def test_check_and_apply_once_major_bump_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_check_now(**_: Any) -> dict:
        return {
            "status": "skipped",
            "current": "0.9.0",
            "latest": "v1.0.0",
            "reason": "refusing major bump 0.9.0 → v1.0.0",
        }

    out = auto_updater.check_and_apply_once(_check=fake_check_now)
    assert out["reason"] == "major_bump_refused"
    assert out["latest_version"] == "v1.0.0"


def test_check_and_apply_once_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_check_now(**_: Any) -> dict:
        return {
            "status": "error",
            "current": "0.5.0",
            "latest": None,
            "reason": "github fetch failed: ConnectionRefusedError",
        }

    out = auto_updater.check_and_apply_once(_check=fake_check_now)
    assert out["ok"] is False
    assert out["applied"] is False
    assert out["restarting"] is False
    # 'github fetch failed' has no 'download' substring so it falls into
    # the default branch — surface the raw reason string.
    assert out["reason"].startswith("github fetch failed")


def test_check_and_apply_once_download_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_check_now(**_: Any) -> dict:
        return {
            "status": "error",
            "current": "0.5.0",
            "latest": "v0.6.0",
            "reason": "download failed: TimeoutError",
        }

    out = auto_updater.check_and_apply_once(_check=fake_check_now)
    assert out["ok"] is False
    assert out["reason"] == "download_failed"


def test_check_and_apply_once_probe_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_check_now(**_: Any) -> dict:
        raise RuntimeError("disk full")

    out = auto_updater.check_and_apply_once(_check=fake_check_now)
    assert out["ok"] is False
    assert out["applied"] is False
    assert out["restarting"] is False
    assert "probe_crashed" in out["reason"]


# ─────────── WSClient round-trip ───────────


class _FakeWS:
    """Minimal websocket-compatible fake — collects every outbound frame
    and iterates a fixed inbound queue exactly once."""

    def __init__(self, incoming: list[str]) -> None:
        self.sent: list[str] = []
        self._incoming = list(incoming)
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self) -> _FakeWS:
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_ws_check_update_replies_with_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentflow_computer_mcp import ws_client as ws_mod

    expected = {
        "ok": True,
        "current_version": "0.5.0",
        "latest_version": "0.5.0",
        "applied": False,
        "restarting": False,
        "reason": "up_to_date",
    }

    def fake_check_and_apply_once(**_: Any) -> dict:
        return expected

    monkeypatch.setattr(ws_mod, "_CHECK_AND_APPLY_ONCE", fake_check_and_apply_once)

    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def tool_handler(name: str, args: dict[str, Any]) -> Any:
        return {}

    client = WSClient(cfg, tool_handler, [])
    fake = _FakeWS([json.dumps({"type": "check_update", "id": "req-42"})])
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()
    # Give the spawned task a tick to run.
    await asyncio.sleep(0.05)

    payloads = [json.loads(s) for s in fake.sent]
    acks = [p for p in payloads if p.get("type") == "check_update_result"]
    assert len(acks) == 1
    assert acks[0]["id"] == "req-42"
    assert acks[0]["result"] == expected


@pytest.mark.asyncio
async def test_ws_check_update_handles_probe_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentflow_computer_mcp import ws_client as ws_mod

    def boom(**_: Any) -> dict:
        raise RuntimeError("github offline")

    monkeypatch.setattr(ws_mod, "_CHECK_AND_APPLY_ONCE", boom)

    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def tool_handler(name: str, args: dict[str, Any]) -> Any:
        return {}

    client = WSClient(cfg, tool_handler, [])
    fake = _FakeWS([json.dumps({"type": "check_update", "id": "req-77"})])
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()
    await asyncio.sleep(0.05)

    payloads = [json.loads(s) for s in fake.sent]
    acks = [p for p in payloads if p.get("type") == "check_update_result"]
    assert len(acks) == 1
    assert acks[0]["id"] == "req-77"
    assert acks[0]["result"]["ok"] is False
    assert "probe_crashed" in acks[0]["result"]["reason"]
