"""Tests for `WsLogHandler`.

Covers level filter (INFO ignored), drop-oldest under queue pressure,
flush-on-publisher-attach, exc_info text capture, and per-frame
truncation.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from agentflow_computer_mcp.ws_log_uploader import (
    DEFAULT_MAX_QUEUE,
    MESSAGE_TRUNCATE_CHARS,
    WsLogHandler,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


def _make_record(level: int, msg: str, *, exc_info: Any = None, name: str = "test") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=10,
        msg=msg,
        args=None,
        exc_info=exc_info,
        func="t",
    )


def test_info_records_are_ignored() -> None:
    h = WsLogHandler()
    h.emit(_make_record(logging.INFO, "noise"))
    h.emit(_make_record(logging.DEBUG, "noise2"))
    assert h.queue_size() == 0


def test_warning_and_above_queue_up() -> None:
    h = WsLogHandler()
    h.emit(_make_record(logging.WARNING, "warn-msg"))
    h.emit(_make_record(logging.ERROR, "err-msg"))
    h.emit(_make_record(logging.CRITICAL, "crit-msg"))
    assert h.queue_size() == 3


def test_drain_on_publisher_attach() -> None:
    h = WsLogHandler()
    h.emit(_make_record(logging.ERROR, "queued-while-offline"))
    sent: list[dict[str, Any]] = []
    h.set_publisher(sent.append)
    assert h.queue_size() == 0
    assert len(sent) == 1
    assert sent[0]["type"] == "device_log"
    assert sent[0]["level"] == "ERROR"
    assert sent[0]["message"] == "queued-while-offline"


def test_publisher_receives_realtime_records_after_attach() -> None:
    h = WsLogHandler()
    sent: list[dict[str, Any]] = []
    h.set_publisher(sent.append)
    h.emit(_make_record(logging.ERROR, "live"))
    assert len(sent) == 1
    assert sent[0]["message"] == "live"


def test_drop_oldest_when_queue_is_full() -> None:
    h = WsLogHandler(max_queue=3)
    for i in range(5):
        h.emit(_make_record(logging.ERROR, f"msg-{i}"))
    assert h.queue_size() == 3
    assert h.dropped_count() == 2
    sent: list[dict[str, Any]] = []
    h.set_publisher(sent.append)
    # Oldest two were dropped; we keep msg-2..msg-4.
    assert [p["message"] for p in sent] == ["msg-2", "msg-3", "msg-4"]


def test_publisher_detach_pauses_drain() -> None:
    h = WsLogHandler()
    sent: list[dict[str, Any]] = []
    h.set_publisher(sent.append)
    h.set_publisher(None)
    h.emit(_make_record(logging.ERROR, "during-blackout"))
    assert len(sent) == 0
    assert h.queue_size() == 1
    h.set_publisher(sent.append)
    assert [p["message"] for p in sent] == ["during-blackout"]


def test_exc_info_text_captured() -> None:
    h = WsLogHandler()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()
    h.emit(_make_record(logging.ERROR, "with-exc", exc_info=exc_info))
    sent: list[dict[str, Any]] = []
    h.set_publisher(sent.append)
    assert "exc_info_text" in sent[0]
    assert "RuntimeError: boom" in sent[0]["exc_info_text"]


def test_message_truncation() -> None:
    h = WsLogHandler()
    long = "a" * (MESSAGE_TRUNCATE_CHARS + 500)
    h.emit(_make_record(logging.ERROR, long))
    sent: list[dict[str, Any]] = []
    h.set_publisher(sent.append)
    assert sent[0]["message"].endswith("…[truncated]")
    assert len(sent[0]["message"]) <= MESSAGE_TRUNCATE_CHARS + len("…[truncated]")


def test_default_max_queue() -> None:
    h = WsLogHandler()
    for i in range(DEFAULT_MAX_QUEUE + 50):
        h.emit(_make_record(logging.ERROR, f"x-{i}"))
    assert h.queue_size() == DEFAULT_MAX_QUEUE
    assert h.dropped_count() == 50


def test_publisher_exception_re_queues_surviving_records() -> None:
    h = WsLogHandler()
    for i in range(3):
        h.emit(_make_record(logging.ERROR, f"m-{i}"))

    calls: list[dict[str, Any]] = []

    def flaky(payload: dict[str, Any]) -> None:
        calls.append(payload)
        if len(calls) == 2:
            raise RuntimeError("ws closed mid-flush")

    h.set_publisher(flaky)
    # First two delivered; second raised → remaining one back in queue.
    assert len(calls) == 2
    assert h.queue_size() >= 1
