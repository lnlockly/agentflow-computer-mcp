from __future__ import annotations

from pathlib import Path

import pytest

from agentflow_computer_mcp.config import Scope
from agentflow_computer_mcp.scope import ScopeDenied
from agentflow_computer_mcp.tools import fs as fs_tool


def test_read_writes_round_trip(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = tmp_path / "hello.txt"
    fs_tool.write(str(target), "hi there", scope)

    result = fs_tool.read(str(target), scope)
    assert result["content"] == "hi there"
    assert result["encoding"] == "utf-8"
    assert result["size_bytes"] == 8


def test_read_denied_outside_allow(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    with pytest.raises(ScopeDenied):
        fs_tool.read("/etc/hosts", scope)


def test_list_dir(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()

    result = fs_tool.list_dir(str(tmp_path), scope)
    names = {e["name"] for e in result["entries"]}
    assert names == {"a.txt", "sub"}


def test_write_binary_base64(tmp_path: Path) -> None:
    import base64
    scope = Scope(allow_paths=(str(tmp_path),))
    payload = b"\x00\x01\x02\x03binary"
    encoded = base64.b64encode(payload).decode("ascii")

    target = tmp_path / "blob.bin"
    fs_tool.write(str(target), encoded, scope, encoding="base64")
    assert target.read_bytes() == payload
