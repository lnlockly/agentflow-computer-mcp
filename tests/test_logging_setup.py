"""Tests for `logging_setup.init_logging` and `log_dir`.

Cover per-platform directory resolution, mkdir tolerance, rotation
respecting maxBytes, and idempotent re-init.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest import mock

import pytest

from agentflow_computer_mcp.logging_setup import (
    LOG_FILENAME,
    init_logging,
    log_dir,
)


@pytest.fixture(autouse=True)
def _reset_root_logger() -> None:
    # Snapshot + restore so test order does not leak handlers.
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    for handler in saved_handlers:
        root.removeHandler(handler)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_log_dir_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert log_dir() == Path(r"C:\Users\test\AppData\Local") / "AgentFlow" / "logs"


def test_log_dir_windows_falls_back_to_appdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    assert log_dir() == Path(r"C:\Users\test\AppData\Roaming") / "AgentFlow" / "logs"


def test_log_dir_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    home = Path.home()
    assert log_dir() == home / "Library" / "Logs" / "AgentFlow"


def test_log_dir_linux_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert log_dir() == Path.home() / ".local" / "state" / "agentflow" / "logs"


def test_log_dir_linux_xdg_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert log_dir() == tmp_path / "agentflow" / "logs"


def test_init_logging_creates_file_and_writes(tmp_path: Path) -> None:
    log_path = init_logging("DEBUG", log_directory=tmp_path)
    assert log_path == tmp_path / LOG_FILENAME
    logging.getLogger("test_init_logging").error("hello world")
    # Flush handlers explicitly — RotatingFileHandler does not flush per emit.
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert log_path is not None
    body = log_path.read_text(encoding="utf-8")
    assert "hello world" in body
    assert "ERROR" in body


def test_init_logging_skips_file_on_unwritable_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point at a dir whose mkdir blows up — daemon should still come up
    # with stderr-only logging instead of crashing on boot.
    bad_dir = tmp_path / "nope"

    def _boom(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("read-only fs")

    with mock.patch.object(Path, "mkdir", _boom):
        log_path = init_logging("INFO", log_directory=bad_dir)
    assert log_path is None
    # Stderr handler is still attached.
    handlers = logging.getLogger().handlers
    assert any(isinstance(h, logging.StreamHandler) for h in handlers)


def test_init_logging_force_replaces_existing_handlers(tmp_path: Path) -> None:
    init_logging("INFO", log_directory=tmp_path)
    first_count = len(logging.getLogger().handlers)
    init_logging("INFO", log_directory=tmp_path)
    second_count = len(logging.getLogger().handlers)
    # Two handlers (stream + file) each time, not stacking.
    assert first_count == second_count == 2


def test_rotation_respects_max_bytes(tmp_path: Path) -> None:
    log_path = init_logging(
        "INFO",
        log_directory=tmp_path,
        max_bytes=500,  # tiny on purpose so we trigger rotation fast
        backup_count=2,
    )
    assert log_path is not None
    logger = logging.getLogger("rotation_test")
    payload = "x" * 100
    # Write enough to force at least one rotation.
    for _ in range(20):
        logger.error(payload)
    for handler in logging.getLogger().handlers:
        handler.flush()
    # At least one rotated file lands next to the live one.
    rotated = list(tmp_path.glob(f"{LOG_FILENAME}*"))
    assert len(rotated) >= 2, f"expected rotation, got {rotated}"
    # Live file must not exceed maxBytes by more than one record-worth.
    assert log_path.stat().st_size < 500 * 2
