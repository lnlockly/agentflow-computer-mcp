from __future__ import annotations

import io
from contextlib import redirect_stdout
from unittest.mock import patch

from agentflow_computer_mcp import __version__
from agentflow_computer_mcp.desktop_cli import _resolve_api_key, build_parser, main


def test_build_parser_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--port", "9999"])
    assert args.port == 9999
    args = parser.parse_args(["drive", "hello world"])
    assert args.task == "hello world"
    args = parser.parse_args(["health"])
    assert args.cmd == "health"


def test_version_subcommand_prints_pkg_version() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["version"])
    assert rc == 0
    assert buf.getvalue().strip() == __version__


def test_tools_subcommand_lists_af_and_desktop() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["tools"])
    out = buf.getvalue()
    assert rc == 0
    assert "screen_capture" in out
    assert "af_list_devices" in out
    assert "task_complete" in out


def test_resolve_api_key_prefers_cli_value() -> None:
    assert _resolve_api_key("cli-key") == "cli-key"


def test_resolve_api_key_reads_env(monkeypatch) -> None:
    monkeypatch.delenv("AGENTFLOW_API_KEY", raising=False)
    monkeypatch.setenv("AF_API_KEY", "env-key")
    with patch("agentflow_computer_mcp.desktop_cli.load_auth") as m:
        m.return_value = type("A", (), {"api_key": ""})()
        assert _resolve_api_key(None) == "env-key"


def test_resolve_api_key_falls_back_to_auth_file(monkeypatch) -> None:
    monkeypatch.delenv("AGENTFLOW_API_KEY", raising=False)
    monkeypatch.delenv("AF_API_KEY", raising=False)
    with patch("agentflow_computer_mcp.desktop_cli.load_auth") as m:
        m.return_value = type("A", (), {"api_key": "file-key"})()
        assert _resolve_api_key(None) == "file-key"


def test_main_no_args_prints_help() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main([])
    assert rc == 1
    assert "agentflow-desktop" in buf.getvalue()
