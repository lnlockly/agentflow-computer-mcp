"""Unit tests for ``_tail_and_stream_agent_log``.

The real flow tails ``OPENCODE_LOG_FILE`` line by line and POSTs batches
to ``/daemon-log/projects/:id/agent-log`` (CF-safe alias of the
internal route — see PR #894). We fake the file with a
temp directory + manual writes, fake the HTTP POST with a capturing
callable, and assert the batching produces the expected shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentflow_computer_mcp.driver.tools import agent_brief as ab


class FakeHttpPoster:
    """Records every POST as (url, parsed_body, headers)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(
        self, url: str, data: bytes, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        parsed = json.loads(data.decode("utf-8"))
        self.calls.append((url, parsed, headers))
        return 200, b'{"ok":true}'


@pytest.fixture(autouse=True)
def _internal_secret(monkeypatch):
    monkeypatch.setenv("AF_INTERNAL_API_SECRET", "test-internal-secret")
    monkeypatch.setenv("AF_API_URL", "https://agentflow.website")


def _write_log(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()


def test_tailer_batches_25_lines_into_10_10_5(tmp_path):
    """25 lines + flush_interval longer than the test runtime → batches
    of {10, 10, 5}. The third batch comes from the final-drain pass in the
    ``finally`` block after the fake pid reads dead."""
    log_path = tmp_path / "opencode.log"
    _write_log(log_path, "\n".join(f"line-{i}" for i in range(25)) + "\n")

    poster = FakeHttpPoster()

    ab._tail_and_stream_agent_log(
        project_id=42,
        log_path=str(log_path),
        opencode_pid=999_999,
        batch_size=10,
        flush_interval_sec=999.0,  # disable the time-based flush
        timeout_sec=10_000.0,
        poll_sleep_sec=0.0,
        http_post=poster,
        pid_alive=lambda _pid: False,
    )

    # 3 batches expected: 10, 10, 5.
    sizes = [len(c[1]["lines"]) for c in poster.calls]
    assert sizes == [10, 10, 5], f"expected [10,10,5], got {sizes}"

    # First batch carries the first 10 lines, third has lines 20-24.
    first_lines = [entry["line"] for entry in poster.calls[0][1]["lines"]]
    assert first_lines == [f"line-{i}" for i in range(10)]
    last_lines = [entry["line"] for entry in poster.calls[2][1]["lines"]]
    assert last_lines == [f"line-{i}" for i in range(20, 25)]


def test_tailer_url_uses_normalised_api_base(tmp_path, monkeypatch):
    """Bare-host AF_API_URL must be suffixed with /_agents — without the
    prefix the public ingress 404s the request (lesson from PR #105)."""
    monkeypatch.setenv("AF_API_URL", "https://agentflow.website")
    log_path = tmp_path / "opencode.log"
    _write_log(log_path, "single line\n")

    poster = FakeHttpPoster()
    ab._tail_and_stream_agent_log(
        project_id=7,
        log_path=str(log_path),
        opencode_pid=None,
        batch_size=5,
        flush_interval_sec=0.0,
        timeout_sec=1.0,
        poll_sleep_sec=0.0,
        http_post=poster,
        pid_alive=lambda _pid: False,
    )

    assert poster.calls, "expected at least one POST"
    url = poster.calls[0][0]
    assert url == "https://agentflow.website/_agents/daemon-log/projects/7/agent-log", url


def test_tailer_sends_secret_header(tmp_path):
    log_path = tmp_path / "opencode.log"
    _write_log(log_path, "one\n")

    poster = FakeHttpPoster()
    ab._tail_and_stream_agent_log(
        project_id=1,
        log_path=str(log_path),
        opencode_pid=None,
        batch_size=1,
        flush_interval_sec=0.0,
        timeout_sec=1.0,
        poll_sleep_sec=0.0,
        http_post=poster,
        pid_alive=lambda _pid: False,
    )
    assert poster.calls, "no batch flushed"
    headers = poster.calls[0][2]
    assert headers.get("x-agentflow-secret") == "test-internal-secret"
    assert headers.get("content-type") == "application/json"


def test_tailer_skips_when_secret_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("AF_INTERNAL_API_SECRET", raising=False)
    log_path = tmp_path / "opencode.log"
    _write_log(log_path, "ignored\n")

    poster = FakeHttpPoster()
    ab._tail_and_stream_agent_log(
        project_id=1,
        log_path=str(log_path),
        opencode_pid=None,
        batch_size=1,
        flush_interval_sec=0.0,
        timeout_sec=1.0,
        poll_sleep_sec=0.0,
        http_post=poster,
        pid_alive=lambda _pid: False,
    )
    assert poster.calls == []


def test_tailer_skips_when_log_file_missing(tmp_path):
    """Missing log path → warning + early exit, no POST."""
    log_path = tmp_path / "never-created.log"
    poster = FakeHttpPoster()
    # Speed the file-wait loop with a fake clock that races past the 5s
    # deadline immediately.
    clock = iter([0.0, 100.0, 200.0, 300.0, 400.0, 500.0])

    def fake_now() -> float:
        return next(clock)

    ab._tail_and_stream_agent_log(
        project_id=1,
        log_path=str(log_path),
        opencode_pid=None,
        batch_size=1,
        flush_interval_sec=0.0,
        timeout_sec=1.0,
        poll_sleep_sec=0.0,
        http_post=poster,
        pid_alive=lambda _pid: False,
        now=fake_now,
    )
    assert poster.calls == []


def test_tailer_drops_empty_lines_client_side(tmp_path):
    log_path = tmp_path / "opencode.log"
    _write_log(log_path, "real one\n\n   \nreal two\n")

    poster = FakeHttpPoster()
    ab._tail_and_stream_agent_log(
        project_id=99,
        log_path=str(log_path),
        opencode_pid=None,
        batch_size=10,
        flush_interval_sec=0.0,
        timeout_sec=1.0,
        poll_sleep_sec=0.0,
        http_post=poster,
        pid_alive=lambda _pid: False,
    )
    assert len(poster.calls) == 1
    payload = poster.calls[0][1]
    lines = [entry["line"] for entry in payload["lines"]]
    assert lines == ["real one", "real two"]


def test_is_pid_alive_handles_none_and_dead_pid():
    assert ab._is_pid_alive(None) is True
    assert ab._is_pid_alive(0) is True
    dead_pid = 99_999_999
    if ab._is_pid_alive(dead_pid):
        pytest.skip(f"pid {dead_pid} happens to be alive — skipping")
    assert ab._is_pid_alive(dead_pid) is False
