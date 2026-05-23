"""`agentflow goal create` POSTs to /me/autonomous/goals."""
from __future__ import annotations

from typer.testing import CliRunner

from agentflow_computer_mcp.cli.main import app


def test_goal_create_posts(monkeypatch) -> None:
    from agentflow_computer_mcp.cli import goal as goal_mod

    captured: dict = {}

    def fake_post(path, body, **kwargs):
        captured["path"] = path
        captured["body"] = body
        return {"id": "g42", "title": body["title"]}

    monkeypatch.setattr(goal_mod.rest_client, "post", fake_post)
    monkeypatch.setattr(
        goal_mod.rest_client, "resolve_api_key", lambda explicit=None: "af_live_test_key"
    )

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["goal", "create", "Launch MVP", "--metric", "revenue", "--target", "1000"],
    )
    assert res.exit_code == 0, res.output
    assert captured["path"] == "/me/autonomous/goals"
    assert captured["body"]["title"] == "Launch MVP"
    assert captured["body"]["metric"] == "revenue"
    assert captured["body"]["target"] == 1000.0
    assert "g42" in res.output
