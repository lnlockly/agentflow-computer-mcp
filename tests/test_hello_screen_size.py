"""Tests for the WS `hello` screen-size report.

The daemon advertises its logical screen size on `hello` so the server can
scale the cabinet's normalized 0..1 Drive clicks to device pixels. The probe
is best-effort: a headless host with no display must degrade to `None` (server
falls back to its default), never crash the handshake.
"""
from __future__ import annotations

import agentflow_computer_mcp.ws_client as ws_client


def test_probe_returns_positive_dims_or_none() -> None:
    result = ws_client._probe_screen_size()
    if result is not None:
        assert set(result) == {"w", "h"}
        assert result["w"] > 0 and result["h"] > 0
        assert isinstance(result["w"], int) and isinstance(result["h"], int)


def test_probe_swallows_backend_errors(monkeypatch) -> None:
    class _Boom:
        def screen_size(self):  # noqa: ANN202
            raise RuntimeError("no display")

    monkeypatch.setattr(
        "agentflow_computer_mcp.platform.backend", _Boom(), raising=False
    )
    # Must not raise — a failed probe simply omits `screen` from hello.
    assert ws_client._probe_screen_size() is None


def test_probe_none_backend(monkeypatch) -> None:
    monkeypatch.setattr(
        "agentflow_computer_mcp.platform.backend", None, raising=False
    )
    assert ws_client._probe_screen_size() is None


def test_probe_maps_backend_tuple_to_wh(monkeypatch) -> None:
    class _Fake:
        def screen_size(self):  # noqa: ANN202
            return (1512, 982)

    monkeypatch.setattr(
        "agentflow_computer_mcp.platform.backend", _Fake(), raising=False
    )
    assert ws_client._probe_screen_size() == {"w": 1512, "h": 982}
