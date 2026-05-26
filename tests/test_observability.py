"""Smoke tests for the observability module.

The real wire-up sends to GlitchTip — these tests just verify the
no-op-without-DSN contract + the init/is_enabled state machine.
"""

from __future__ import annotations

import importlib

from agentflow_computer_mcp import observability as obs


def test_noop_when_dsn_absent(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    # Reset module state so the test is independent of others.
    importlib.reload(obs)
    obs.init_sentry()
    assert obs.is_enabled() is False
    obs.report_event("ping", level="info", tag="x")  # must not raise


def test_init_is_idempotent(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    importlib.reload(obs)
    obs.init_sentry()
    obs.init_sentry()
    obs.init_sentry()
    assert obs.is_enabled() is False


def test_empty_dsn_treated_as_absent(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "")
    importlib.reload(obs)
    obs.init_sentry()
    assert obs.is_enabled() is False
