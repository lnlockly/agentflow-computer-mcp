"""`agentflow budget` formats spend / cap."""
from __future__ import annotations

from typer.testing import CliRunner

from agentflow_computer_mcp.cli.main import app


def test_budget_formats(monkeypatch) -> None:
    from agentflow_computer_mcp.cli import budget as budget_mod

    def fake_get(path, **kwargs):
        assert path == "/me/autonomous/budget"
        return {"spent_usd": 0.42, "cap_usd": 5.0}

    monkeypatch.setattr(budget_mod.rest_client, "get", fake_get)

    runner = CliRunner()
    res = runner.invoke(app, ["budget"])
    assert res.exit_code == 0, res.output
    assert "$0.42 / $5.00 (8%)" in res.output


def test_budget_not_authed(monkeypatch) -> None:
    from agentflow_computer_mcp.cli import budget as budget_mod
    from agentflow_computer_mcp.cli.rest_client import NotAuthenticated

    def fake_get(path, **kwargs):
        raise NotAuthenticated("не авторизован; запусти `agentflow login`")

    monkeypatch.setattr(budget_mod.rest_client, "get", fake_get)

    runner = CliRunner()
    res = runner.invoke(app, ["budget"])
    assert res.exit_code == 4
    assert "не авторизован" in res.output
