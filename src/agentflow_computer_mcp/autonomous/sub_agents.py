"""Sub-agent intent recorder.

Phase 0 stub. ``spawn`` only writes a row in the ``sub_agents`` table
so the planner can express «I want a research-only agent to look into
X», then a later phase actually starts a worker process or remote agent
to consume that row.

Keeping the schema + API stable now means Phase 2 just adds an executor
behind ``mark_running``/``mark_done`` without churning callers.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import DEFAULT_DB_PATH, connect, init_db


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def _ensure(db_path: Path | str) -> sqlite3.Connection:
    init_db(db_path)
    return connect(db_path)


def spawn(
    role: str,
    brief: str,
    parent_id: int | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Record a sub-agent invocation request. Returns its row id."""
    conn = _ensure(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sub_agents(parent_id, role, brief) VALUES (?, ?, ?)",
            (parent_id, role, brief),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_pending(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    conn = _ensure(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM sub_agents WHERE status='pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_running(sub_agent_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    conn = _ensure(db_path)
    try:
        conn.execute(
            "UPDATE sub_agents SET status='running' WHERE id=?", (int(sub_agent_id),)
        )
        conn.commit()
    finally:
        conn.close()


def mark_done(
    sub_agent_id: int,
    result: dict[str, Any] | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    conn = _ensure(db_path)
    try:
        conn.execute(
            "UPDATE sub_agents SET status='done', result_json=?, completed_at=? WHERE id=?",
            (json.dumps(result or {}), _utcnow(), int(sub_agent_id)),
        )
        conn.commit()
    finally:
        conn.close()
