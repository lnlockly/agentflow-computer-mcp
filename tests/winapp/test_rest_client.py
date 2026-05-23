"""Verify `winapp.cloud` parses goals + budget payloads correctly.

We monkeypatch `rest_client.get` so the test has no network.
"""
from __future__ import annotations

import pytest

from agentflow_computer_mcp.cli import rest_client
from agentflow_computer_mcp.winapp import cloud


def test_fetch_goals_parses_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sample = {
        "goals": [
            {"id": "g1", "title": "First", "status": "running"},
            {"id": "g2", "title": "Second", "status": "pending"},
        ]
    }
    monkeypatch.setattr(cloud.rest_client, "get", lambda path: sample)
    authed, goals = cloud.fetch_goals()
    assert authed is True
    assert [g.id for g in goals] == ["g1", "g2"]
    assert goals[0].status == "running"


def test_fetch_goals_handles_bare_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cloud.rest_client,
        "get",
        lambda path: [{"id": "g1", "title": "Only", "status": "done"}],
    )
    authed, goals = cloud.fetch_goals()
    assert authed is True
    assert goals[0].title == "Only"


def test_fetch_goals_limit_truncates() -> None:
    pass  # exercised below via fetch_goals(limit=2)


def test_fetch_goals_limits_to_n(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cloud.rest_client,
        "get",
        lambda path: {"goals": [{"id": f"g{i}", "title": f"T{i}", "status": "x"} for i in range(10)]},
    )
    _, goals = cloud.fetch_goals(limit=3)
    assert len(goals) == 3


def test_fetch_goals_returns_unauthed_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: str):
        raise rest_client.ServerError(401, "no")

    monkeypatch.setattr(cloud.rest_client, "get", boom)
    authed, goals = cloud.fetch_goals()
    assert authed is False
    assert goals == ()


def test_fetch_goals_returns_unauthed_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: str):
        raise rest_client.NotAuthenticated("nope")

    monkeypatch.setattr(cloud.rest_client, "get", boom)
    authed, goals = cloud.fetch_goals()
    assert authed is False


def test_fetch_budget_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud.rest_client, "get", lambda path: {"spent_usd": 1.5, "cap_usd": 4.0})
    authed, budget = cloud.fetch_budget()
    assert authed is True
    assert budget.spent == 1.5
    assert budget.cap == 4.0


def test_fetch_budget_defaults_on_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud.rest_client, "get", lambda path: {})
    authed, budget = cloud.fetch_budget()
    assert authed is True
    assert budget.spent == 0.0
    assert budget.cap == 0.0


def test_fetch_budget_returns_unauthed_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: str):
        raise rest_client.ServerError(403, "no")

    monkeypatch.setattr(cloud.rest_client, "get", boom)
    authed, _ = cloud.fetch_budget()
    assert authed is False
