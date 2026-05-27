"""Unit tests for the daemon-push preview heartbeat loop.

Phase-3 priority #0 (2026-05-27): replaces host-side preview polling
with daemon-push heartbeat. The loop probes ``localhost:$PORT`` every
30s and POSTs alive/dead to the backend's ``/preview-alive`` endpoint.

Tests fake every side effect (probe, HTTP poster, sleep) so they pass
in <1s without a real network or dev server.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentflow_computer_mcp.driver.tools import agent_brief as ab


class FakeProbe:
    """Scripted ``_http_probe`` replacement.

    ``responses`` is a list of booleans returned in order. After the list
    is exhausted, the last value repeats — that way a test like "fail
    forever" doesn't need to provision N copies of False.
    """

    def __init__(self, responses: list[bool]):
        self.responses = list(responses)
        self.calls: list[int] = []

    def __call__(self, port: int) -> bool:
        self.calls.append(port)
        if not self.responses:
            return False
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class FakePoster:
    """Records every ``_post_heartbeat`` HTTP POST."""

    def __init__(self, status: int = 200):
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, data: bytes, headers: dict[str, str]):
        import json

        body = json.loads(data.decode("utf-8"))
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        return self.status, b"{}"


@pytest.fixture
def env(monkeypatch):
    """Heartbeat loop short-circuits without AF_INTERNAL_API_SECRET."""
    monkeypatch.setenv("AF_INTERNAL_API_SECRET", "test-secret")
    monkeypatch.setenv("AF_API_URL", "https://agentflow.website")


def test_alive_probe_posts_alive_true_and_resets_miss_counter(env):
    probe = FakeProbe([True, True, True])
    poster = FakePoster()

    ab._heartbeat_preview_loop(
        project_id=1234,
        port=3000,
        heartbeat_interval_sec=0.0,  # no real sleeping in tests
        http_probe=probe,
        http_post=poster,
        sleep=lambda _s: None,
        max_iterations=3,
    )

    # Every iteration probed once and POSTed alive=true.
    assert len(probe.calls) == 3
    assert all(p == 3000 for p in probe.calls)
    assert len(poster.calls) == 3
    for call in poster.calls:
        assert call["body"] == {"alive": True, "port": 3000}
        assert call["url"].endswith("/daemon-log/projects/1234/preview-alive")
        assert call["headers"]["x-agentflow-secret"] == "test-secret"
        # CF-safe UA — see _post_heartbeat docstring for the repro.
        assert call["headers"]["user-agent"].startswith("curl/")


def test_three_consecutive_misses_post_alive_false_once(env):
    # Three misses → one dead heartbeat. Loop keeps running, additional
    # misses do NOT spam more dead heartbeats.
    probe = FakeProbe([False, False, False, False, False])
    poster = FakePoster()

    ab._heartbeat_preview_loop(
        project_id=42,
        port=3000,
        heartbeat_interval_sec=0.0,
        http_probe=probe,
        http_post=poster,
        sleep=lambda _s: None,
        max_iterations=5,
    )

    # Exactly one dead heartbeat at the 3rd miss; the 4th and 5th miss
    # short-circuit on dead_reported=True.
    dead_posts = [c for c in poster.calls if c["body"]["alive"] is False]
    alive_posts = [c for c in poster.calls if c["body"]["alive"] is True]
    assert len(dead_posts) == 1
    assert len(alive_posts) == 0
    # The single dead heartbeat carries the dev port for the backend log.
    assert dead_posts[0]["body"] == {"alive": False, "port": 3000}


def test_recovery_after_dead_posts_alive_again(env):
    # Sequence: miss, miss, miss (dead), miss (silent), ok (alive again),
    # miss, miss, miss (dead again) — verifies dead_reported resets on ok.
    probe = FakeProbe([False, False, False, False, True, False, False, False])
    poster = FakePoster()

    ab._heartbeat_preview_loop(
        project_id=9,
        port=4000,
        heartbeat_interval_sec=0.0,
        http_probe=probe,
        http_post=poster,
        sleep=lambda _s: None,
        max_iterations=8,
    )

    alive_posts = [c for c in poster.calls if c["body"]["alive"] is True]
    dead_posts = [c for c in poster.calls if c["body"]["alive"] is False]
    # One ok in the middle → one alive heartbeat.
    assert len(alive_posts) == 1
    # Two distinct outages → two dead heartbeats.
    assert len(dead_posts) == 2


def test_single_miss_does_not_post_dead(env):
    # 1 miss < threshold (3) → no dead heartbeat, no alive heartbeat
    # (the probe is False, so nothing to report yet).
    probe = FakeProbe([False])
    poster = FakePoster()

    ab._heartbeat_preview_loop(
        project_id=1,
        port=3000,
        heartbeat_interval_sec=0.0,
        http_probe=probe,
        http_post=poster,
        sleep=lambda _s: None,
        max_iterations=1,
    )

    # Probe ran once, no POST issued.
    assert len(probe.calls) == 1
    assert len(poster.calls) == 0


def test_loop_skips_when_internal_secret_missing(monkeypatch):
    # Safeguard mirroring _watch_and_report_clone_status: without the
    # internal secret we cannot authenticate, so the loop returns early.
    monkeypatch.delenv("AF_INTERNAL_API_SECRET", raising=False)
    probe = FakeProbe([True])
    poster = FakePoster()

    ab._heartbeat_preview_loop(
        project_id=1,
        port=3000,
        heartbeat_interval_sec=0.0,
        http_probe=probe,
        http_post=poster,
        sleep=lambda _s: None,
        max_iterations=5,
    )

    # Nothing happened — neither probe nor POST.
    assert probe.calls == []
    assert poster.calls == []


def test_alive_recovery_resets_consecutive_misses(env):
    # 2 misses (below threshold), then ok, then 2 more misses — the counter
    # was reset by the ok, so we do NOT cross the threshold and no dead
    # heartbeat fires. This is the "transient blip" case where polling
    # would have flapped.
    probe = FakeProbe([False, False, True, False, False])
    poster = FakePoster()

    ab._heartbeat_preview_loop(
        project_id=7,
        port=3000,
        heartbeat_interval_sec=0.0,
        http_probe=probe,
        http_post=poster,
        sleep=lambda _s: None,
        max_iterations=5,
    )

    dead_posts = [c for c in poster.calls if c["body"]["alive"] is False]
    alive_posts = [c for c in poster.calls if c["body"]["alive"] is True]
    assert len(dead_posts) == 0
    assert len(alive_posts) == 1
