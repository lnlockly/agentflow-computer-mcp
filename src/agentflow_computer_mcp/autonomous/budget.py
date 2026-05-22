"""Budget tracker for LLM + tool + time spend.

Every LLM call should funnel through ``record_llm_cost`` so the agent
can throttle itself when daily spend exceeds the user-set cap. Prices
are hard-coded per-model in ``_PRICE_TABLE`` (USD per million tokens)
and may drift; treat the ledger as an estimate, not invoice-grade.

When the daily cap trips, ``alert_if_over`` enqueues a system task via
the public ``/me/devices/<id>/dispatch_task`` endpoint so the user sees
the alert on whichever device they happen to be at. In tests this is
a no-op because no device_id is wired.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schema import DEFAULT_DB_PATH, connect, init_db

# USD per 1M tokens (input, output). Source: public price pages as of 2026-05.
# These are estimates; the ledger isn't an invoice.
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (0.80, 4.0),
    "claude-haiku-4-6": (0.80, 4.0),
    "gpt-5.5": (5.0, 20.0),
    "gpt-image-1": (10.0, 40.0),
}

_DEFAULT_PRICE = (3.0, 15.0)  # Sonnet-ish fallback


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%fZ")


def _today() -> str:
    """UTC-day string. The ledger stores `created_at` in UTC, so the
    daily-sum filter must match in UTC too — using local `date.today()`
    would split the day in half for non-UTC users."""
    return datetime.now(UTC).date().isoformat()


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_p, out_p = _PRICE_TABLE.get(model, _DEFAULT_PRICE)
    return (input_tokens / 1_000_000.0) * in_p + (output_tokens / 1_000_000.0) * out_p


def record_llm_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    action_id: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> float:
    """Insert a row and return the estimated USD spend for this call."""
    cost = estimate_cost(model, int(input_tokens), int(output_tokens))
    note = f"model={model} in={input_tokens} out={output_tokens}"
    init_db(db_path)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO budget_ledger(kind, amount_usd, action_id, note) "
            "VALUES ('llm', ?, ?, ?)",
            (float(cost), action_id, note),
        )
        conn.commit()
    finally:
        conn.close()
    return cost


def record_tool_cost(
    amount_usd: float,
    note: str = "",
    action_id: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO budget_ledger(kind, amount_usd, action_id, note) "
            "VALUES ('tool', ?, ?, ?)",
            (float(amount_usd), action_id, note),
        )
        conn.commit()
    finally:
        conn.close()


def today_spent(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, float]:
    """Sum today's ledger grouped by kind. Always returns llm/tool/time keys."""
    init_db(db_path)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind, COALESCE(SUM(amount_usd), 0) AS total "
            "FROM budget_ledger WHERE substr(created_at, 1, 10) = ? "
            "GROUP BY kind",
            (_today(),),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, float] = {"llm": 0.0, "tool": 0.0, "time": 0.0}
    for r in rows:
        out[r["kind"]] = float(r["total"])
    out["total"] = round(sum(out.values()), 6)
    return out


def alert_if_over(
    daily_cap_usd: float = 5.0,
    *,
    device_id: str | None = None,
    api_key: str | None = None,
    base_url: str = "https://agentflow.website/_agents",
    db_path: Path | str = DEFAULT_DB_PATH,
    http_post: Any = None,  # injectable for tests
) -> dict[str, Any]:
    """If today's spend exceeds the cap, enqueue an alert task on the device.

    Returns a dict describing what happened — useful for logging:
      {triggered: bool, spent: float, cap: float, dispatched: bool, error?: str}
    """
    spent = today_spent(db_path)
    total = float(spent["total"])
    if total <= daily_cap_usd:
        return {"triggered": False, "spent": total, "cap": daily_cap_usd, "dispatched": False}

    if not device_id or not api_key:
        return {
            "triggered": True,
            "spent": total,
            "cap": daily_cap_usd,
            "dispatched": False,
            "error": "no device_id/api_key wired",
        }

    body = json.dumps(
        {
            "kind": "system_alert",
            "title": "Autonomous budget cap hit",
            "body": (
                f"Today's spend ${total:.2f} > cap ${daily_cap_usd:.2f}. "
                "Pausing non-essential planner calls until midnight UTC."
            ),
            "created_at": _utcnow(),
        }
    ).encode()

    if http_post is None:
        def http_post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

    url = f"{base_url.rstrip('/')}/me/devices/{device_id}/dispatch_task"
    headers = {
        "x-api-key": api_key,
        "content-type": "application/json",
        "user-agent": "agentflow-desktop-autonomous/0.1",
    }
    try:
        status, _ = http_post(url, body, headers)
        dispatched = 200 <= status < 300
        err: str | None = None if dispatched else f"http {status}"
    except Exception as exc:  # network: best-effort, never raise
        dispatched = False
        err = str(exc)

    return {
        "triggered": True,
        "spent": total,
        "cap": daily_cap_usd,
        "dispatched": dispatched,
        "error": err,
    }
