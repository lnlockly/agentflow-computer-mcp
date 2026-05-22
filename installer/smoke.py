"""Pre-build smoke for the self-contained installer.

The .exe now bundles Python + agentflow_computer_mcp + every dep, so
the old «stream pip output for 30s» check no longer applies. Instead
this script verifies the three pieces that still need wiring:

1. `parse_invite` happy + reject paths (still the user-facing entry).
2. `write_auth_file` writes the expected shape into a temp HOME.
3. The bundled daemon entry point imports cleanly — i.e. the same
   module the PyInstaller spec hard-pins is actually importable from
   the current Python.

Post-build a second gate runs against the artifact itself:

    agentflow-desktop-setup.exe --daemon --selftest

The release workflow asserts that exits 0 within 30 seconds. Together
the two gates catch (a) source-level regressions and (b) PyInstaller
collection misses.

Exit code 0 = release safe to publish.
Exit code != 0 = release MUST be blocked.

Run manually:
    python installer/smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))
sys.path.insert(0, str(ROOT / "src"))

from setup_gui import (  # noqa: E402  (sys.path manip)
    parse_invite,
    write_auth_file,
)


def log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[smoke] FAIL — {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def check_invite_roundtrip() -> None:
    log("invite-code parse: happy + reject paths")
    creds = parse_invite(
        "eyJrIjoiYWZfbGl2ZV90ZXN0IiwiZCI6IjAwMDAtMDAwMC0wMDAwIiwidCI6ImFmdF90ZXN0In0"
    )
    assert creds["api_key"] == "af_live_test"
    assert creds["device_id"] == "0000-0000-0000"
    assert creds["device_token"] == "aft_test"
    for bad, why in [
        ("", "empty"),
        ("not-base64!!", "junk"),
        # missing token prefix
        ("eyJrIjoiYWZfbGl2ZSIsImQiOiIwMCIsInQiOiJ4eHgifQ", "bad token prefix"),
    ]:
        try:
            parse_invite(bad)
        except ValueError:
            continue
        fail(f"parse_invite should have rejected: {why}")


def check_auth_file_shape() -> None:
    log("write_auth_file: shape + on-disk presence")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["USERPROFILE"] = tmp
        os.environ["HOME"] = tmp
        path = write_auth_file(
            {
                "api_key": "af_live_smoketest",
                "device_id": "smoke-uuid",
                "device_token": "aft_smoketest",
            }
        )
        if not path.exists():
            fail(f"auth.json not written to {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("api_key", "device_id", "enrollment_token", "ws_url"):
            if key not in data:
                fail(f"auth.json missing key: {key}")
        if data["api_key"] != "af_live_smoketest":
            fail("auth.json api_key mismatch")


def check_daemon_entrypoint_imports() -> None:
    """The bundle relies on `from agentflow_computer_mcp.desktop_cli
    import main` working in the frozen runtime. If that import breaks
    here, it breaks in the .exe too — catch it early."""
    log("daemon entry point: import agentflow_computer_mcp.desktop_cli")
    try:
        from agentflow_computer_mcp.desktop_cli import main  # noqa: F401
    except Exception as exc:
        fail(f"cannot import daemon entry point: {exc}")
    log("  ok — daemon main() is importable")


def check_autonomous_skeleton() -> None:
    """Import every autonomous module, init a temp DB, exercise
    decompose_goal + plan_today + recall + budget against a stubbed LLM.

    No real network calls; the stub returns a hard-coded JSON payload
    so we verify wiring, not LLM quality.
    """
    log("autonomous: schema + planner + memory + budget against mock LLM")
    try:
        from agentflow_computer_mcp.autonomous import (
            budget as af_budget,
            cli as af_cli,
            memory as af_memory,
            planner as af_planner,
            schema as af_schema,
            sub_agents as af_sub_agents,
            wake_cycle as af_wake_cycle,
        )
    except Exception as exc:
        fail(f"cannot import autonomous package: {exc}")
    _ = (af_cli, af_sub_agents, af_wake_cycle)  # imported for side-effect coverage

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "autonomous.db"
        path = af_schema.init_db(db_path)
        if not path.exists():
            fail(f"init_db did not create {path}")

        # Round-trip every table with a single INSERT to confirm schema works.
        conn = af_schema.connect(db_path)
        try:
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        for required in (
            "goals",
            "milestones",
            "daily_plans",
            "lessons",
            "skills",
            "budget_ledger",
            "sub_agents",
        ):
            if required not in tables:
                fail(f"autonomous schema missing table: {required}")

        goal_id = af_planner.add_goal(
            title="Test goal",
            description="smoke",
            target_metric="usd_earned",
            target_value=1000.0,
            deadline_at="2027-01-01",
            db_path=db_path,
        )

        canned_milestones = [
            {
                "title": "M1: discover niche",
                "success_criteria": "shortlist of 3 niches",
                "scheduled_for": "2026-06-01",
            },
            {
                "title": "M2: build landing",
                "success_criteria": "lighthouse > 90",
                "scheduled_for": "2026-07-01",
            },
            {
                "title": "M3: first paying customer",
                "success_criteria": "stripe charge > $0",
                "scheduled_for": "2026-08-01",
            },
        ]

        def mock_llm(system: str, user: str, opts: dict) -> dict:
            # planner asks for a JSON array on decompose, JSON object on plan_today.
            if "milestones" in system.lower():
                return {
                    "text": json.dumps(canned_milestones),
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "model": "mock-haiku",
                }
            if "daily planner" in system.lower():
                plan = {
                    "date": "2026-05-23",
                    "tasks": [
                        {
                            "tool": "browser",
                            "objective": "research niche X",
                            "acceptance": "5 competitors listed",
                        }
                    ],
                }
                return {
                    "text": json.dumps(plan),
                    "input_tokens": 50,
                    "output_tokens": 80,
                    "model": "mock-haiku",
                }
            if "evaluate the day" in system.lower():
                payload = {
                    "score": 8,
                    "reflection": "decent",
                    "lessons": [
                        {"kind": "workflow", "topic": "niche research", "summary": "use SE only"}
                    ],
                    "skills": [
                        {"name": "niche-shortlist", "when_to_use": "research niche", "recipe": {"steps": ["a", "b"]}}
                    ],
                }
                return {
                    "text": json.dumps(payload),
                    "input_tokens": 30,
                    "output_tokens": 50,
                    "model": "mock-haiku",
                }
            return {"text": "{}", "input_tokens": 0, "output_tokens": 0, "model": "mock-haiku"}

        inserted = af_planner.decompose_goal(goal_id, mock_llm, db_path=db_path)
        if len(inserted) != 3:
            fail(f"decompose_goal expected 3 milestones, got {len(inserted)}")

        # Budget ledger should have recorded the call.
        spent = af_budget.today_spent(db_path=db_path)
        if spent["llm"] <= 0:
            fail(f"budget ledger empty after decompose; got {spent}")

        plan = af_planner.plan_today(inserted[0]["id"], mock_llm, db_path=db_path)
        if "id" not in plan or "plan" not in plan:
            fail(f"plan_today returned bad shape: {plan}")

        reflection = af_planner.reflect_on_day(
            plan["id"],
            "completed task 1, blocked on task 2",
            mock_llm,
            db_path=db_path,
        )
        if reflection["score"] != 8:
            fail(f"reflect_on_day expected score 8, got {reflection}")
        if reflection["lessons_recorded"] != 1:
            fail("expected 1 lesson recorded")

        recalled = af_memory.recall("niche research", db_path=db_path)
        if not recalled:
            fail("memory.recall returned no lessons after reflect")

        skills = af_memory.top_skills("research niche", db_path=db_path)
        if not skills:
            fail("memory.top_skills returned no entries")

        sub_id = af_sub_agents.spawn(
            role="researcher", brief="find 5 niches", db_path=db_path
        )
        pending = af_sub_agents.list_pending(db_path=db_path)
        if not any(p["id"] == sub_id for p in pending):
            fail("sub_agents.spawn row not found in list_pending")

        # Budget alert: a dispatch with no api_key should NOT raise and should
        # mark dispatched=False instead.
        af_budget.record_tool_cost(99.0, note="forced over", db_path=db_path)
        alert = af_budget.alert_if_over(
            daily_cap_usd=1.0,
            device_id=None,
            api_key=None,
            db_path=db_path,
        )
        if not alert["triggered"]:
            fail("alert_if_over should fire after 99 USD tool cost over 1 USD cap")
        if alert["dispatched"]:
            fail("alert_if_over should NOT dispatch with missing credentials")

    log("  ok — autonomous skeleton wired end-to-end")


def main() -> None:
    log("starting smoke for installer/setup_gui.py")
    check_invite_roundtrip()
    check_auth_file_shape()
    check_daemon_entrypoint_imports()
    check_autonomous_skeleton()
    log("ALL GREEN")


if __name__ == "__main__":
    main()
