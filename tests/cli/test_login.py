"""`agentflow login` writes auth.json mode 0600 and masks key in echo."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from typer.testing import CliRunner

from agentflow_computer_mcp.cli.main import app


def test_login_writes_auth_file(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    # also re-point the AUTH_FILE constant by reloading config indirection
    from agentflow_computer_mcp import auth as auth_mod
    from agentflow_computer_mcp import config as config_mod
    from agentflow_computer_mcp.cli import auth_cli as auth_cli_mod

    auth_file = home / ".agentflow" / "auth.json"
    monkeypatch.setattr(config_mod, "AGENTFLOW_DIR", home / ".agentflow")
    monkeypatch.setattr(config_mod, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth_mod, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth_cli_mod, "AUTH_FILE", auth_file)

    runner = CliRunner()
    res = runner.invoke(app, ["login", "--api-key", "af_live_secret_1234567890"])
    assert res.exit_code == 0, res.output
    assert auth_file.exists()

    payload = json.loads(auth_file.read_text())
    assert payload["api_key"] == "af_live_secret_1234567890"

    if os.name == "posix":
        mode = stat.S_IMODE(auth_file.stat().st_mode)
        assert mode == 0o600

    assert "af_live_secret_1234567890" not in res.output
    assert "af_live_" in res.output
