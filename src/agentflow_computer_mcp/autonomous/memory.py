"""Lesson + skill memory for the autonomous loop.

Two append-mostly stores keyed by topic/name:

- ``lessons``: free-form summaries the planner emits during reflection.
- ``skills``: named workflows the agent can pull when ``when_to_use``
  matches the current task. Each carries success/fail counters so a
  skill that keeps failing decays out of recommendations.

Retrieval ranking is intentionally dumb in v1: substring + token-overlap
score, no embeddings. Embeddings get bolted on once the dataset is big
enough that BM25 plateaus.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import DEFAULT_DB_PATH, connect, init_db

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def _ensure(db_path: Path | str) -> sqlite3.Connection:
    init_db(db_path)
    return connect(db_path)


def learn(
    kind: str,
    topic: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    score: int = 0,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Insert a lesson. Returns the new row id."""
    conn = _ensure(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO lessons(kind, topic, summary, payload_json, score) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, topic, summary, json.dumps(payload or {}), int(score)),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def recall(
    topic: str,
    limit: int = 8,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Return up to `limit` lessons ranked by token overlap with `topic`.

    Tie-break by recency (newer wins) so a stale lesson never beats a
    fresh one with the same overlap.
    """
    conn = _ensure(db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, topic, summary, payload_json, score, created_at "
            "FROM lessons ORDER BY id DESC LIMIT 2000"
        ).fetchall()
    finally:
        conn.close()

    query_tokens = _tokenize(topic)
    if not query_tokens:
        return [dict(r) for r in rows[:limit]]

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for r in rows:
        text = f"{r['topic']} {r['summary']}"
        overlap = len(query_tokens & _tokenize(text))
        if overlap == 0:
            continue
        scored.append((overlap, int(r["id"]), dict(r)))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [t[2] for t in scored[:limit]]


def record_skill(
    name: str,
    when_to_use: str,
    recipe: dict[str, Any],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Upsert a skill by name. New row → success_count=1, existing row → unchanged counters."""
    conn = _ensure(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO skills(name, when_to_use, recipe_json, success_count, last_used_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  when_to_use=excluded.when_to_use, "
            "  recipe_json=excluded.recipe_json",
            (name, when_to_use, json.dumps(recipe), _utcnow()),
        )
        conn.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = conn.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()
        return int(row["id"])
    finally:
        conn.close()


def record_skill_outcome(
    skill_id: int,
    success: bool,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    conn = _ensure(db_path)
    try:
        column = "success_count" if success else "fail_count"
        conn.execute(
            f"UPDATE skills SET {column} = {column} + 1, last_used_at = ? WHERE id = ?",
            (_utcnow(), int(skill_id)),
        )
        conn.commit()
    finally:
        conn.close()


def top_skills(
    when_to_use_query: str,
    limit: int = 5,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Skills whose `when_to_use` overlaps `when_to_use_query`, ranked by
    (overlap, success-rate, recency). Skills with fail_count > 2*success_count
    are filtered out — failing skills should retire themselves.
    """
    conn = _ensure(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, when_to_use, recipe_json, success_count, fail_count, last_used_at "
            "FROM skills"
        ).fetchall()
    finally:
        conn.close()

    query_tokens = _tokenize(when_to_use_query)
    scored: list[tuple[int, float, str, dict[str, Any]]] = []
    for r in rows:
        s = int(r["success_count"])
        f = int(r["fail_count"])
        if f > 0 and f > 2 * s:
            continue  # decayed
        overlap = len(query_tokens & _tokenize(r["when_to_use"] or ""))
        if overlap == 0 and query_tokens:
            continue
        total = s + f
        success_rate = (s / total) if total else 0.0
        last = r["last_used_at"] or ""
        scored.append((overlap, success_rate, last, dict(r)))

    scored.sort(key=lambda t: (-t[0], -t[1], t[2]), reverse=False)
    # primary -overlap (higher better) — sort key returns negative, so ascending order is correct
    scored.sort(key=lambda t: (-t[0], -t[1], -ord(t[2][0]) if t[2] else 0))
    return [t[3] for t in scored[:limit]]
