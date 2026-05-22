"""Self-modification queue.

The driver's LLM can request a code change against the
``agentflow-computer-mcp`` repo via ``selfmod_request_change``. Requests are
appended to a JSONL queue at ``~/.agentflow-desktop/selfmod-queue.jsonl`` and
picked up by :mod:`selfmod_worker` which spawns ``claude -p`` headless to do
the work.

All file paths are deterministic. No external network here — that lives in
the worker.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

Urgency = Literal["low", "normal", "high"]
Status = Literal["queued", "in_progress", "merged", "pr_opened", "rejected", "failed", "throttled", "cancelled"]

QUEUE_DIR_ENV = "AGENTFLOW_DESKTOP_HOME"
DEFAULT_QUEUE_DIR = "~/.agentflow-desktop"
QUEUE_FILENAME = "selfmod-queue.jsonl"

# 1 request per 15 minutes globally. Excess marked throttled.
MIN_INTERVAL_SECONDS = 15 * 60

_LOCK = threading.Lock()


def queue_dir() -> Path:
    override = os.environ.get(QUEUE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_QUEUE_DIR).expanduser()


def queue_path() -> Path:
    return queue_dir() / QUEUE_FILENAME


def _ensure_dir() -> None:
    queue_dir().mkdir(parents=True, exist_ok=True)


def _read_all() -> list[dict[str, Any]]:
    p = queue_path()
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write_all(rows: list[dict[str, Any]]) -> None:
    _ensure_dir()
    tmp = queue_path().with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(queue_path())


def _append(row: dict[str, Any]) -> None:
    _ensure_dir()
    with queue_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _last_accepted_ts(rows: list[dict[str, Any]]) -> float:
    accepted = [r for r in rows if r.get("status") != "throttled"]
    if not accepted:
        return 0.0
    return max(float(r.get("created_at", 0)) for r in accepted)


def request_change(
    reason: str,
    suggested_change: str,
    urgency: Urgency = "normal",
) -> dict[str, Any]:
    """Append a change request to the queue.

    Returns ``{"request_id", "queued": bool, "status"}``. When rate-limited
    the entry is still written with ``status="throttled"`` so the caller has
    an audit trail.
    """
    if not reason.strip():
        raise ValueError("reason is required")
    if not suggested_change.strip():
        raise ValueError("suggested_change is required")
    if urgency not in ("low", "normal", "high"):
        raise ValueError(f"invalid urgency: {urgency!r}")

    with _LOCK:
        rows = _read_all()
        now = time.time()
        last = _last_accepted_ts(rows)
        throttled = (now - last) < MIN_INTERVAL_SECONDS

        request_id = f"sm-{uuid.uuid4().hex[:12]}"
        row: dict[str, Any] = {
            "request_id": request_id,
            "reason": reason.strip(),
            "suggested_change": suggested_change.strip(),
            "urgency": urgency,
            "created_at": now,
            "status": "throttled" if throttled else "queued",
            "pr_url": None,
            "error": None,
            "updated_at": now,
        }
        if throttled:
            row["error"] = (
                f"rate limit: last accepted request {int(now - last)}s ago "
                f"(min interval {MIN_INTERVAL_SECONDS}s)"
            )
        _append(row)
        return {
            "request_id": request_id,
            "queued": not throttled,
            "status": row["status"],
        }


def list_recent(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` entries, newest first."""
    if limit < 1:
        return []
    with _LOCK:
        rows = _read_all()
    rows.sort(key=lambda r: float(r.get("created_at", 0)), reverse=True)
    return rows[:limit]


def update_status(
    request_id: str,
    status: Status,
    *,
    pr_url: str | None = None,
    error: str | None = None,
) -> bool:
    """Patch one row by request_id. Returns True if a row was updated."""
    with _LOCK:
        rows = _read_all()
        found = False
        for row in rows:
            if row.get("request_id") == request_id:
                row["status"] = status
                row["updated_at"] = time.time()
                if pr_url is not None:
                    row["pr_url"] = pr_url
                if error is not None:
                    row["error"] = error
                found = True
                break
        if found:
            _write_all(rows)
        return found


def cancel(request_id: str) -> bool:
    """Mark a queued request as cancelled. No-op if already in progress."""
    with _LOCK:
        rows = _read_all()
        for row in rows:
            if row.get("request_id") == request_id:
                if row.get("status") not in ("queued", "throttled"):
                    return False
                row["status"] = "cancelled"
                row["updated_at"] = time.time()
                _write_all(rows)
                return True
        return False


def requeue(request_id: str) -> bool:
    """Reset a rejected/failed/throttled request back to queued."""
    with _LOCK:
        rows = _read_all()
        for row in rows:
            if row.get("request_id") == request_id:
                if row.get("status") in ("queued", "in_progress"):
                    return False
                row["status"] = "queued"
                row["error"] = None
                row["updated_at"] = time.time()
                _write_all(rows)
                return True
        return False


def pop_next_queued() -> dict[str, Any] | None:
    """Atomically claim the oldest queued entry and mark it in_progress."""
    with _LOCK:
        rows = _read_all()
        rows_by_age = sorted(rows, key=lambda r: float(r.get("created_at", 0)))
        for row in rows_by_age:
            if row.get("status") == "queued":
                row["status"] = "in_progress"
                row["updated_at"] = time.time()
                _write_all(rows)
                return dict(row)
        return None
