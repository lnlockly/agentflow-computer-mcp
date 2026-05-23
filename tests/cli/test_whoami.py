"""`agentflow whoami` masks the api key and lists devices."""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agentflow_computer_mcp.cli.main import app


def test_whoami_masks_key(monkeypatch, tmp_path: Path) -> None:
    from agentflow_computer_mcp.cli import auth_cli as auth_cli_mod
    from agentflow_computer_mcp.config import Auth

    monkeypatch.setattr(
        auth_cli_mod, "load_auth",
        lambda: Auth(api_key="af_live_abcdef1234567890", device_id="dev1"),
    )

    def fake_get(path, **kwargs):
        assert path == "/me"
        return {
            "id": 7,
            "email": "user@example.com",
            "devices": [{"id": "dev1", "label": "Mac", "status": "online"}],
        }

    monkeypatch.setattr(auth_cli_mod.rest_client, "get", fake_get)

    runner = CliRunner()
    res = runner.invoke(app, ["whoami"])
    assert res.exit_code == 0, res.output
    assert "af_live_" in res.output
    assert "abcdef1234567890" not in res.output
    assert "#7" in res.output
    assert "dev1" in res.output
