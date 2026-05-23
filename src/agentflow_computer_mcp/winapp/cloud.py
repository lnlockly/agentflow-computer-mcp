"""Fetch goals + budget via the REST helpers from `cli.rest_client`.

Both functions degrade gracefully: missing auth.json, missing network, or
non-2xx responses all collapse into `None` / empty tuples — the menu
shows "Не авторизован" instead of crashing the tray.
"""
from __future__ import annotations

from typing import Any

from ..cli import rest_client
from .state import Budget, GoalRow


def _coerce_goals(data: Any, limit: int = 5) -> tuple[GoalRow, ...]:
    if data is None:
        return ()
    if isinstance(data, dict):
        inner = data.get("goals") or data.get("items") or data.get("results") or []
    elif isinstance(data, list):
        inner = data
    else:
        return ()
    rows: list[GoalRow] = []
    for item in inner:
        if not isinstance(item, dict):
            continue
        rows.append(
            GoalRow(
                id=str(item.get("id", "")),
                title=str(item.get("title", "") or "(без названия)"),
                status=str(item.get("status", "") or ""),
            )
        )
    return tuple(rows[:limit])


def fetch_goals(limit: int = 5) -> tuple[bool, tuple[GoalRow, ...]]:
    """Return (authenticated, goals). `authenticated=False` masks all 401-likes."""
    try:
        data = rest_client.get("/me/autonomous/goals")
    except rest_client.NotAuthenticated:
        return False, ()
    except rest_client.ServerError as exc:
        if exc.status in (401, 403):
            return False, ()
        return True, ()
    except (OSError, RuntimeError):
        return True, ()
    return True, _coerce_goals(data, limit=limit)


def fetch_budget() -> tuple[bool, Budget]:
    try:
        data = rest_client.get("/me/autonomous/budget")
    except rest_client.NotAuthenticated:
        return False, Budget()
    except rest_client.ServerError as exc:
        if exc.status in (401, 403):
            return False, Budget()
        return True, Budget()
    except (OSError, RuntimeError):
        return True, Budget()
    spent = float((data or {}).get("spent_usd") or 0.0)
    cap = float((data or {}).get("cap_usd") or 0.0)
    return True, Budget(spent=spent, cap=cap)
