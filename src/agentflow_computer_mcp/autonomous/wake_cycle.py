"""Daily wake cycle: at wake-hour produce today's plan and dispatch
tasks to the user's device; at sleep-hour reflect and mark milestones
complete.

Triggering is done by ``run_forever`` which checks every minute whether
the current local time matches wake_hour or sleep_hour and fires once.
The loop is deliberately simple (no `schedule` lib dep) because we only
have two fixed daily events.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .planner import LlmFn, plan_today, reflect_on_day
from .schema import DEFAULT_DB_PATH, connect, init_db

log = logging.getLogger(__name__)


HttpPostFn = Callable[[str, bytes, dict[str, str]], tuple[int, bytes]]


def _default_http_post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%fZ")


def _ensure(db_path: Path | str) -> sqlite3.Connection:
    init_db(db_path)
    return connect(db_path)


def find_current_milestone(
    goal_id: int,
    now: datetime | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """First non-completed milestone for `goal_id`, ordered by scheduled_for
    then id. Returns None if none pending.
    """
    conn = _ensure(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM milestones WHERE goal_id=? AND status!='completed' "
            "ORDER BY (scheduled_for IS NULL), scheduled_for ASC, id ASC LIMIT 1",
            (int(goal_id),),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def dispatch_task_to_device(
    api_key: str,
    device_id: str,
    task: dict[str, Any],
    base_url: str = "https://agentflow.website/_agents",
    http_post: HttpPostFn | None = None,
) -> dict[str, Any]:
    """POST a single autonomous task to /me/devices/<id>/dispatch_task.

    `task` is expected to have at minimum {tool, objective, acceptance}.
    Returns {dispatched: bool, status: int, error?: str}.
    """
    post = http_post or _default_http_post
    body = json.dumps(
        {
            "kind": "autonomous_task",
            "created_at": _utcnow(),
            "task": task,
        }
    ).encode()
    url = f"{base_url.rstrip('/')}/me/devices/{device_id}/dispatch_task"
    headers = {
        "x-api-key": api_key,
        "content-type": "application/json",
        "user-agent": "agentflow-desktop-autonomous/0.1",
    }
    try:
        status, _ = post(url, body, headers)
    except Exception as exc:
        return {"dispatched": False, "status": 0, "error": str(exc)}
    return {
        "dispatched": 200 <= status < 300,
        "status": status,
        "error": None if 200 <= status < 300 else f"http {status}",
    }


def wake(
    now: datetime,
    owner_user_id: int,
    *,
    api_key: str,
    device_id: str | None,
    llm_fn: LlmFn,
    db_path: Path | str = DEFAULT_DB_PATH,
    http_post: HttpPostFn | None = None,
) -> dict[str, Any]:
    """One wake-up tick.

    For each active goal:
      1. Find the current milestone (skip goal if none).
      2. Call plan_today.
      3. Dispatch each task to the device (best-effort, errors logged).

    `owner_user_id` is kept for future multi-tenant fanout but unused in v1.
    Returns a summary dict — what got planned and dispatched.
    """
    _ = owner_user_id  # reserved
    from .planner import list_active_goals  # avoid early import cycle

    summary: dict[str, Any] = {"now": now.isoformat(), "goals": []}
    for goal in list_active_goals(db_path=db_path):
        gid = int(goal["id"])
        milestone = find_current_milestone(gid, now=now, db_path=db_path)
        entry: dict[str, Any] = {"goal_id": gid, "goal_title": goal["title"]}
        if not milestone:
            entry["skipped"] = "no_pending_milestone"
            summary["goals"].append(entry)
            continue

        try:
            plan_record = plan_today(
                milestone["id"], llm_fn, db_path=db_path, today=now.date().isoformat()
            )
        except Exception as exc:
            entry["error"] = f"plan_today: {exc}"
            summary["goals"].append(entry)
            continue

        entry["plan_id"] = plan_record["id"]
        entry["milestone_id"] = milestone["id"]
        tasks = plan_record["plan"].get("tasks", []) or []
        entry["task_count"] = len(tasks)

        if not device_id:
            entry["dispatched"] = 0
            entry["note"] = "no device_id configured; plan stored only"
            summary["goals"].append(entry)
            continue

        dispatched = 0
        errors: list[str] = []
        for t in tasks:
            res = dispatch_task_to_device(
                api_key, device_id, t, http_post=http_post
            )
            if res["dispatched"]:
                dispatched += 1
            elif res.get("error"):
                errors.append(res["error"])
        entry["dispatched"] = dispatched
        if errors:
            entry["errors"] = errors[:5]
        summary["goals"].append(entry)
    return summary


def sleep_reflect(
    today: str | None,
    observed_outcomes_by_plan: dict[int, str],
    llm_fn: LlmFn,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """End-of-day reflection.

    `observed_outcomes_by_plan` maps daily_plan.id → free-form outcome text.
    For each plan reflected with score >= 7, mark its milestone completed
    if its scheduled_for is in the past (best-effort heuristic).
    """
    today = today or datetime.now(UTC).date().isoformat()
    results: list[dict[str, Any]] = []
    for plan_id, outcomes in observed_outcomes_by_plan.items():
        try:
            r = reflect_on_day(int(plan_id), outcomes, llm_fn, db_path=db_path)
        except Exception as exc:
            results.append({"daily_plan_id": int(plan_id), "error": str(exc)})
            continue
        results.append(r)

        if r["score"] >= 7:
            conn = _ensure(db_path)
            try:
                row = conn.execute(
                    "SELECT milestone_id FROM daily_plans WHERE id=?", (int(plan_id),)
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE milestones SET status='completed', completed_at=? "
                        "WHERE id=? AND status!='completed'",
                        (_utcnow(), int(row["milestone_id"])),
                    )
                    conn.commit()
            finally:
                conn.close()
    return results


def run_forever(
    *,
    api_key: str,
    device_id: str | None,
    llm_fn: LlmFn,
    owner_user_id: int = 0,
    wake_hour: int = 7,
    wake_minute: int = 30,
    sleep_hour: int = 23,
    sleep_minute: int = 0,
    db_path: Path | str = DEFAULT_DB_PATH,
    tick_seconds: int = 60,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Block forever, firing `wake` once per day at wake_hour:wake_minute.

    Intentionally local-naive (uses `datetime.now()` without tz) so the
    user's local clock drives the schedule. `clock` is injectable for
    deterministic tests but the loop is not unit-tested directly.
    """
    clock = clock or datetime.now
    last_wake_date: str | None = None
    last_sleep_date: str | None = None

    while True:
        try:
            now = clock()
            today_str = now.date().isoformat()
            if (
                now.hour == wake_hour
                and now.minute == wake_minute
                and last_wake_date != today_str
            ):
                log.info("autonomous wake @ %s", now.isoformat())
                wake(
                    now,
                    owner_user_id,
                    api_key=api_key,
                    device_id=device_id,
                    llm_fn=llm_fn,
                    db_path=db_path,
                )
                last_wake_date = today_str

            if (
                now.hour == sleep_hour
                and now.minute == sleep_minute
                and last_sleep_date != today_str
            ):
                log.info("autonomous sleep-reflect @ %s (no outcomes wired yet)", now.isoformat())
                # Phase 0: we don't yet have an outcome harvester. Leave the
                # call site here so wiring is one diff away.
                last_sleep_date = today_str
        except Exception as exc:  # never let the loop die
            log.exception("wake_cycle tick failed: %s", exc)
        time.sleep(max(1, int(tick_seconds)))
