"""Command-line entry point for the autonomous subsystem.

Run as ``python -m agentflow_computer_mcp.autonomous.cli <subcommand>``.

Subcommands:
  init                 Create the SQLite tables under ~/.agentflow/.
  goal add <title>     Insert a new goal (flags: --description, --target-metric,
                       --target-value, --deadline).
  goal list            Show active goals.
  plan --goal=N --decompose
                       Ask the LLM to split goal N into milestones.
  today --goal=N       Plan + dispatch today's tasks for goal N.
  reflect --plan=N --outcomes=<text>
                       Reflect on plan N with the supplied outcome notes.
  status               One-shot dashboard: goals, milestones, today spend.

LLM + device wiring is read from ~/.agentflow/auth.json (api_key,
device_id, ws_url-derived base URL). If auth.json is missing the LLM
subcommands refuse to run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import budget, memory, planner, schema, sub_agents, wake_cycle


def _load_api_key_and_device() -> tuple[str | None, str | None]:
    """Return (api_key, device_id) from ~/.agentflow/auth.json. Either may be None."""
    auth_path = Path.home() / ".agentflow" / "auth.json"
    if not auth_path.exists():
        return None, None
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    return data.get("api_key") or None, data.get("device_id") or None


def _need_llm_fn() -> planner.LlmFn:
    key, _ = _load_api_key_and_device()
    if not key:
        print("[autonomous] no api_key in ~/.agentflow/auth.json", file=sys.stderr)
        sys.exit(2)
    return planner._default_llm_fn(key)


def _cmd_init(_: argparse.Namespace) -> int:
    path = schema.init_db()
    print(f"initialized {path}")
    return 0


def _cmd_goal(args: argparse.Namespace) -> int:
    if args.goal_action == "add":
        gid = planner.add_goal(
            title=args.title,
            description=args.description or "",
            target_metric=args.target_metric or "",
            target_value=args.target_value,
            deadline_at=args.deadline,
        )
        print(json.dumps({"goal_id": gid}))
        return 0
    if args.goal_action == "list":
        rows = planner.list_active_goals()
        print(json.dumps(rows, indent=2, default=str))
        return 0
    print(f"unknown goal subcommand: {args.goal_action}", file=sys.stderr)
    return 2


def _cmd_plan(args: argparse.Namespace) -> int:
    if not args.decompose:
        print("currently only --decompose is implemented", file=sys.stderr)
        return 2
    llm = _need_llm_fn()
    rows = planner.decompose_goal(int(args.goal), llm)
    print(json.dumps(rows, indent=2, default=str))
    return 0


def _cmd_today(args: argparse.Namespace) -> int:
    api_key, device_id = _load_api_key_and_device()
    if not api_key:
        print("[autonomous] no api_key in auth.json", file=sys.stderr)
        return 2
    llm = planner._default_llm_fn(api_key)
    summary = wake_cycle.wake(
        datetime.now(),
        owner_user_id=0,
        api_key=api_key,
        device_id=device_id,
        llm_fn=llm,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


def _cmd_reflect(args: argparse.Namespace) -> int:
    llm = _need_llm_fn()
    res = wake_cycle.sleep_reflect(
        today=None,
        observed_outcomes_by_plan={int(args.plan): args.outcomes or ""},
        llm_fn=llm,
    )
    print(json.dumps(res, indent=2, default=str))
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    goals = planner.list_active_goals()
    spent = budget.today_spent()
    pending_sub = sub_agents.list_pending()
    out: dict[str, Any] = {
        "active_goals": goals,
        "today_spent_usd": spent,
        "pending_sub_agents": pending_sub,
    }
    # quick milestone counts per goal
    counts: dict[int, dict[str, int]] = {}
    conn = schema.connect(schema.DEFAULT_DB_PATH)
    try:
        for g in goals:
            rows = conn.execute(
                "SELECT status, COUNT(*) as c FROM milestones WHERE goal_id=? GROUP BY status",
                (g["id"],),
            ).fetchall()
            counts[int(g["id"])] = {r["status"]: int(r["c"]) for r in rows}
    finally:
        conn.close()
    out["milestone_counts"] = counts
    # recent lessons
    out["recent_lessons"] = memory.recall("", limit=5)
    print(json.dumps(out, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentflow_computer_mcp.autonomous.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create SQLite tables")

    g = sub.add_parser("goal", help="goal CRUD")
    gsub = g.add_subparsers(dest="goal_action", required=True)
    g_add = gsub.add_parser("add")
    g_add.add_argument("title")
    g_add.add_argument("--description", default="")
    g_add.add_argument("--target-metric", default="")
    g_add.add_argument("--target-value", type=float, default=None)
    g_add.add_argument("--deadline", default=None, help="YYYY-MM-DD")
    gsub.add_parser("list")

    pl = sub.add_parser("plan", help="planner ops")
    pl.add_argument("--goal", required=True, type=int)
    pl.add_argument("--decompose", action="store_true")

    td = sub.add_parser("today", help="plan + dispatch today's tasks")
    td.add_argument("--goal", type=int, default=None, help="(reserved; v1 plans all active goals)")

    rf = sub.add_parser("reflect", help="reflect on a daily plan")
    rf.add_argument("--plan", required=True, type=int)
    rf.add_argument("--outcomes", default="")

    sub.add_parser("status", help="dashboard snapshot")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "init":
        return _cmd_init(args)
    if args.cmd == "goal":
        return _cmd_goal(args)
    if args.cmd == "plan":
        return _cmd_plan(args)
    if args.cmd == "today":
        return _cmd_today(args)
    if args.cmd == "reflect":
        return _cmd_reflect(args)
    if args.cmd == "status":
        return _cmd_status(args)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
