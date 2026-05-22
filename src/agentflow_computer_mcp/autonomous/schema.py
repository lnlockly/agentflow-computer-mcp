"""SQLite schema for the autonomous-goals subsystem.

Single DB at ~/.agentflow/autonomous.db. Tables created idempotently
via ``init_db(path)``. All other modules import a connection from here
so they share the same migration semantics.

No ORM by design — sqlite3 + raw SQL is enough for the row-counts we
expect (thousands of lessons, hundreds of milestones, dozens of goals).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".agentflow" / "autonomous.db"


SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        target_metric TEXT NOT NULL DEFAULT '',
        target_value REAL,
        deadline_at TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        parent_goal_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        FOREIGN KEY (parent_goal_id) REFERENCES goals(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status)
    """,
    """
    CREATE TABLE IF NOT EXISTS milestones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        success_criteria TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        scheduled_for TEXT,
        parent_milestone_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        completed_at TEXT,
        FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE CASCADE,
        FOREIGN KEY (parent_milestone_id) REFERENCES milestones(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_milestones_goal ON milestones(goal_id, status, scheduled_for)
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        milestone_id INTEGER NOT NULL,
        plan_json TEXT NOT NULL,
        executed_at TEXT,
        reflection TEXT,
        score INTEGER,
        FOREIGN KEY (milestone_id) REFERENCES milestones(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_daily_plans_date ON daily_plans(date)
    """,
    """
    CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        topic TEXT NOT NULL,
        summary TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        score INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_lessons_topic ON lessons(topic)
    """,
    """
    CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        when_to_use TEXT NOT NULL DEFAULT '',
        recipe_json TEXT NOT NULL DEFAULT '{}',
        success_count INTEGER NOT NULL DEFAULT 0,
        fail_count INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS budget_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        amount_usd REAL NOT NULL,
        action_id TEXT,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_budget_created ON budget_ledger(created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS sub_agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id INTEGER,
        role TEXT NOT NULL,
        brief TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        result_json TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        completed_at TEXT
    )
    """,
)


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a sqlite3 connection with sane defaults.

    - `Path` is parent-mkdir'd so callers can pass a fresh tmp path.
    - `row_factory` returns dict-like rows so callers don't pivot on
      tuple ordering when columns change.
    - `foreign_keys` enforced so cascades fire.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> Path:
    """Create every table + index idempotently. Returns the resolved path."""
    db_path = Path(db_path)
    conn = connect(db_path)
    try:
        for stmt in SCHEMA_SQL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()
    return db_path
