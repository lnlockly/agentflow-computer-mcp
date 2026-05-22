"""LLM-driven planner: goal → milestones → daily plan → reflection.

The planner is the only module that talks to an LLM in Phase 0. All
calls go through ``llm_fn`` (injected) so tests can mock without
touching the network. The default ``llm_fn`` posts to the same
``/llm/v1/messages`` endpoint that the OS-prompt loop uses, with the
``af_live_*`` API key from ``~/.agentflow/auth.json``.

System prompts here are stable English-only strings — the planner
output is structured JSON, not user-facing copy, so localization is
not a Phase 0 concern.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .budget import record_llm_cost
from .schema import DEFAULT_DB_PATH, connect, init_db

DEFAULT_LLM_URL = "https://agentflow.website/_agents/llm/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"  # planner doesn't need Opus by default

LlmFn = Callable[[str, str, dict[str, Any]], dict[str, Any]]
# (system_prompt, user_prompt, opts) → {"text": str, "input_tokens": int, "output_tokens": int}


@dataclass
class LlmCallResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = DEFAULT_MODEL


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%fZ")


def _default_llm_fn(api_key: str, llm_url: str = DEFAULT_LLM_URL) -> LlmFn:
    """Build a default LLM caller bound to the real /llm/v1/messages endpoint."""

    def _call(system: str, user: str, opts: dict[str, Any]) -> dict[str, Any]:
        model = opts.get("model", DEFAULT_MODEL)
        payload = {
            "model": model,
            "max_tokens": int(opts.get("max_tokens", 2048)),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            llm_url,
            data=body,
            headers={
                "x-api-key": api_key,
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "user-agent": "agentflow-desktop-autonomous/0.1",
            },
        )
        with urllib.request.urlopen(req, timeout=int(opts.get("timeout", 120))) as r:
            resp = json.loads(r.read().decode())
        text = ""
        for block in resp.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        usage = resp.get("usage", {}) or {}
        return {
            "text": text,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "model": resp.get("model", model),
        }

    return _call


def _ensure(db_path: Path | str) -> sqlite3.Connection:
    init_db(db_path)
    return connect(db_path)


# --- JSON extraction --------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"(\[.*\]|\{.*\})", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Best-effort JSON parse from LLM output. Try fenced block, then
    the first bare array/object, then the whole string. Raises ValueError
    if nothing parses.
    """
    candidates: list[str] = []
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))
    bare = _BARE_JSON_RE.search(text)
    if bare:
        candidates.append(bare.group(1))
    candidates.append(text.strip())
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no JSON found in LLM output: {text[:200]!r}")


# --- Goal CRUD --------------------------------------------------------

def add_goal(
    title: str,
    description: str = "",
    target_metric: str = "",
    target_value: float | None = None,
    deadline_at: str | None = None,
    parent_goal_id: int | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    conn = _ensure(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO goals(title, description, target_metric, target_value, "
            "deadline_at, parent_goal_id) VALUES (?, ?, ?, ?, ?, ?)",
            (title, description, target_metric, target_value, deadline_at, parent_goal_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_goal(goal_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    conn = _ensure(db_path)
    try:
        row = conn.execute("SELECT * FROM goals WHERE id=?", (int(goal_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_active_goals(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    conn = _ensure(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM goals WHERE status='active' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- decompose_goal ---------------------------------------------------

_DECOMPOSE_SYSTEM = (
    "You break down a long-horizon goal into 5-12 milestones with clear, "
    "binary-checkable success criteria and rough deadlines (ISO-8601 dates). "
    "Each milestone must be necessary and roughly in execution order. "
    "Return ONLY a JSON array of objects: "
    '[{"title": str, "success_criteria": str, "scheduled_for": "YYYY-MM-DD"}, ...]'
)


def decompose_goal(
    goal_id: int,
    llm_fn: LlmFn,
    db_path: Path | str = DEFAULT_DB_PATH,
    model: str = DEFAULT_MODEL,
    track_cost: bool = True,
) -> list[dict[str, Any]]:
    """Ask the LLM to split a goal into milestones, persist them, return rows."""
    goal = get_goal(goal_id, db_path=db_path)
    if not goal:
        raise ValueError(f"goal {goal_id} not found")

    user = (
        f"Goal title: {goal['title']}\n"
        f"Description: {goal['description'] or '(none)'}\n"
        f"Target metric: {goal['target_metric'] or '(none)'}\n"
        f"Target value: {goal['target_value']}\n"
        f"Deadline: {goal['deadline_at'] or '(none)'}\n\n"
        "Produce milestones now."
    )
    result = llm_fn(_DECOMPOSE_SYSTEM, user, {"model": model, "max_tokens": 3000})
    parsed = _extract_json(result["text"])
    if not isinstance(parsed, list):
        raise ValueError(f"decompose_goal expected JSON array, got {type(parsed).__name__}")

    if track_cost:
        record_llm_cost(
            result.get("model", model),
            int(result.get("input_tokens", 0)),
            int(result.get("output_tokens", 0)),
            action_id=f"decompose_goal:{goal_id}",
            db_path=db_path,
        )

    conn = _ensure(db_path)
    inserted: list[dict[str, Any]] = []
    try:
        for m in parsed:
            if not isinstance(m, dict):
                continue
            title = str(m.get("title", "")).strip()
            if not title:
                continue
            criteria = str(m.get("success_criteria", "")).strip()
            scheduled = m.get("scheduled_for")
            cur = conn.execute(
                "INSERT INTO milestones(goal_id, title, success_criteria, scheduled_for) "
                "VALUES (?, ?, ?, ?)",
                (int(goal_id), title, criteria, scheduled),
            )
            inserted.append(
                {
                    "id": int(cur.lastrowid),
                    "goal_id": int(goal_id),
                    "title": title,
                    "success_criteria": criteria,
                    "scheduled_for": scheduled,
                }
            )
        conn.commit()
    finally:
        conn.close()
    return inserted


# --- plan_today -------------------------------------------------------

_PLAN_TODAY_SYSTEM = (
    "You are the daily planner for an autonomous desktop agent. Given a "
    "milestone, produce up to 8 concrete tasks the agent can execute today. "
    "Each task must name the tool category (browser, shell, write_file, "
    "send_message, http) and a one-line objective. "
    "Return ONLY a JSON object: "
    '{"date": "YYYY-MM-DD", "tasks": [{"tool": str, "objective": str, '
    '"acceptance": str}]}'
)


def plan_today(
    milestone_id: int,
    llm_fn: LlmFn,
    db_path: Path | str = DEFAULT_DB_PATH,
    model: str = DEFAULT_MODEL,
    today: str | None = None,
    track_cost: bool = True,
) -> dict[str, Any]:
    """Produce + persist a daily plan for `milestone_id`."""
    conn = _ensure(db_path)
    try:
        row = conn.execute(
            "SELECT m.*, g.title AS goal_title, g.target_metric, g.target_value "
            "FROM milestones m JOIN goals g ON g.id = m.goal_id WHERE m.id = ?",
            (int(milestone_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"milestone {milestone_id} not found")

    today = today or datetime.now(UTC).date().isoformat()
    user = (
        f"Today's date: {today}\n"
        f"Goal: {row['goal_title']} (target {row['target_metric']}={row['target_value']})\n"
        f"Milestone: {row['title']}\n"
        f"Success criteria: {row['success_criteria']}\n"
        f"Milestone deadline: {row['scheduled_for'] or '(none)'}\n\n"
        "Produce today's plan now."
    )
    result = llm_fn(_PLAN_TODAY_SYSTEM, user, {"model": model, "max_tokens": 2000})
    parsed = _extract_json(result["text"])
    if not isinstance(parsed, dict) or "tasks" not in parsed:
        raise ValueError("plan_today expected JSON object with 'tasks' key")

    if track_cost:
        record_llm_cost(
            result.get("model", model),
            int(result.get("input_tokens", 0)),
            int(result.get("output_tokens", 0)),
            action_id=f"plan_today:{milestone_id}",
            db_path=db_path,
        )

    conn = _ensure(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO daily_plans(date, milestone_id, plan_json) VALUES (?, ?, ?)",
            (today, int(milestone_id), json.dumps(parsed)),
        )
        conn.commit()
        plan_id = int(cur.lastrowid)
    finally:
        conn.close()

    return {"id": plan_id, "date": today, "milestone_id": int(milestone_id), "plan": parsed}


# --- reflect_on_day ---------------------------------------------------

_REFLECT_SYSTEM = (
    "You evaluate the day's plan against what actually happened. "
    "Rate execution quality 1-10, extract 1-5 lessons (each: kind in "
    "['workflow','tool','market','self'], topic, summary), and propose "
    "0-3 reusable skills (each: name, when_to_use, recipe as a JSON object). "
    "Return ONLY a JSON object: "
    '{"score": int 1..10, "lessons": [...], "skills": [...], "reflection": str}'
)


def reflect_on_day(
    daily_plan_id: int,
    observed_outcomes: str,
    llm_fn: LlmFn,
    db_path: Path | str = DEFAULT_DB_PATH,
    model: str = DEFAULT_MODEL,
    track_cost: bool = True,
) -> dict[str, Any]:
    """Score the day, persist lessons + skills, update daily_plans.reflection."""
    conn = _ensure(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM daily_plans WHERE id=?", (int(daily_plan_id),)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"daily_plan {daily_plan_id} not found")

    user = (
        f"Daily plan ({row['date']}, milestone {row['milestone_id']}):\n"
        f"{row['plan_json']}\n\n"
        f"Observed outcomes:\n{observed_outcomes}\n\n"
        "Score, extract lessons, propose skills."
    )
    result = llm_fn(_REFLECT_SYSTEM, user, {"model": model, "max_tokens": 2000})
    parsed = _extract_json(result["text"])
    if not isinstance(parsed, dict):
        raise ValueError("reflect_on_day expected JSON object")

    score = int(parsed.get("score", 0) or 0)
    reflection = str(parsed.get("reflection", ""))

    if track_cost:
        record_llm_cost(
            result.get("model", model),
            int(result.get("input_tokens", 0)),
            int(result.get("output_tokens", 0)),
            action_id=f"reflect:{daily_plan_id}",
            db_path=db_path,
        )

    # Persist lessons + skills via memory module (lazy import — avoid cycle).
    from . import memory as _memory

    for lesson in parsed.get("lessons", []) or []:
        if not isinstance(lesson, dict):
            continue
        _memory.learn(
            kind=str(lesson.get("kind", "self")),
            topic=str(lesson.get("topic", "")),
            summary=str(lesson.get("summary", "")),
            payload={"daily_plan_id": int(daily_plan_id)},
            score=score,
            db_path=db_path,
        )
    for skill in parsed.get("skills", []) or []:
        if not isinstance(skill, dict):
            continue
        name = str(skill.get("name", "")).strip()
        if not name:
            continue
        _memory.record_skill(
            name=name,
            when_to_use=str(skill.get("when_to_use", "")),
            recipe=skill.get("recipe", {}) or {},
            db_path=db_path,
        )

    conn = _ensure(db_path)
    try:
        conn.execute(
            "UPDATE daily_plans SET reflection=?, score=?, executed_at=? WHERE id=?",
            (reflection, score, _utcnow(), int(daily_plan_id)),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "daily_plan_id": int(daily_plan_id),
        "score": score,
        "reflection": reflection,
        "lessons_recorded": len(parsed.get("lessons", []) or []),
        "skills_recorded": len(parsed.get("skills", []) or []),
    }
