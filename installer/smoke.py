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
        from agentflow_computer_mcp.autonomous import budget as af_budget
        from agentflow_computer_mcp.autonomous import cli as af_cli
        from agentflow_computer_mcp.autonomous import memory as af_memory
        from agentflow_computer_mcp.autonomous import planner as af_planner
        from agentflow_computer_mcp.autonomous import schema as af_schema
        from agentflow_computer_mcp.autonomous import sub_agents as af_sub_agents
        from agentflow_computer_mcp.autonomous import wake_cycle as af_wake_cycle
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


def check_auto_updater() -> None:
    """Verify the auto-update module imports + the no-op path runs.

    The check_now() call is invoked with an injected fetch() that returns
    a release OLDER than the current bundled version, so the updater MUST
    NOT trigger a download. Any download attempt fails the smoke.
    """
    log("auto_updater: import + mocked older-release no-download path")
    try:
        from agentflow_computer_mcp import __version__ as local_version
        from agentflow_computer_mcp.auto_updater import check_now
    except Exception as exc:
        fail(f"cannot import auto_updater: {exc}")

    download_called = {"hit": False}

    def fake_fetch() -> dict:
        # Return a release tagged at v0.0.1 — older than anything we'd ship.
        return {
            "tag_name": "v0.0.1",
            "body": "sha256: " + ("0" * 64),
            "assets": [
                {
                    "name": "agentflow-desktop-setup.exe",
                    "browser_download_url": "https://example.invalid/setup.exe",
                }
            ],
        }

    def fake_download(url: str, dest) -> None:  # noqa: ARG001, ANN001
        download_called["hit"] = True
        fail("auto_updater attempted a download for an older release")

    def fake_apply(_path) -> None:  # noqa: ANN001
        fail("auto_updater attempted to apply an older release")

    result = check_now(
        fetch=fake_fetch,
        downloader=fake_download,
        apply=fake_apply,
        allow_unfrozen=True,
    )
    if download_called["hit"]:
        fail("downloader called for an older release (should be skipped)")
    if result.get("status") != "current":
        fail(f"expected status=current, got {result!r}")
    log(f"  ok -- auto_updater stays put on {local_version} vs fake v0.0.1")


def check_os_aware_tool_filter() -> None:
    """The driver builds the LLM tool catalog from the current host OS.
    Mac-only tools must not leak onto Windows, and Windows-only tools must
    not leak onto macOS — otherwise the agent will call commands the host
    can't run.
    """
    log("os-aware tool filter: catalog respects current platform")
    from unittest.mock import patch

    from agentflow_computer_mcp.driver import desktop_tools

    # Simulate a Windows host. PowerShell tool should be present; AppleScript
    # tools (chrome_eval / chrome_tabs) should be filtered out.
    with patch("agentflow_computer_mcp.driver.desktop_tools.platform.system", return_value="Windows"):
        names = {t["name"] for t in desktop_tools.all_tool_descriptors()}
        if "powershell_exec" not in names:
            fail("powershell_exec missing from Windows tool catalog")
        if "chrome_eval" in names:
            fail("chrome_eval leaked into Windows tool catalog (mac-only)")
        if "chrome_tabs" in names:
            fail("chrome_tabs leaked into Windows tool catalog (mac-only)")

    # Simulate a Mac host. AppleScript tools should be present; PowerShell
    # tools should be filtered out.
    with patch("agentflow_computer_mcp.driver.desktop_tools.platform.system", return_value="Darwin"):
        names = {t["name"] for t in desktop_tools.all_tool_descriptors()}
        if "chrome_eval" not in names:
            fail("chrome_eval missing from macOS tool catalog")
        if "powershell_exec" in names:
            fail("powershell_exec leaked into macOS tool catalog (windows-only)")
        if "winget_search" in names:
            fail("winget_search leaked into macOS tool catalog (windows-only)")

    # start_app + chrome_open_url are cross-platform — visible on both.
    for sys_name in ("Darwin", "Windows", "Linux"):
        with patch("agentflow_computer_mcp.driver.desktop_tools.platform.system", return_value=sys_name):
            names = {t["name"] for t in desktop_tools.all_tool_descriptors()}
            for cross in ("start_app", "chrome_open_url", "screen_capture", "browser_open"):
                if cross not in names:
                    fail(f"{cross} should be available on {sys_name}")

    log("  ok — Windows hides AppleScript tools, macOS hides PowerShell tools")


def check_os_aware_system_prompt() -> None:
    """The driver loop injects the current host OS into the system prompt
    so the LLM picks the right shell / clipboard / browser commands."""
    log("os-aware system prompt: build_system_prompt includes host OS")
    from agentflow_computer_mcp.driver.loop import HOST_OS, build_system_prompt

    prompt = build_system_prompt("(no windows)", af_tools_present=False)
    if "ОС хоста" not in prompt:
        fail("system prompt missing host-OS context block")
    if HOST_OS not in prompt:
        fail(f"system prompt does not mention HOST_OS={HOST_OS!r}")
    if "Codex" not in prompt:
        fail("system prompt missing Codex / package-manager knowledge block")
    if "Известные подводные камни" not in prompt:
        fail("system prompt missing pitfalls knowledge block")
    log(f"  ok -- prompt declares host OS = {HOST_OS}")


def check_loop_caps_constants() -> None:
    """The driver loop must expose the new step/cost/checkpoint caps as
    module-level constants so ops can introspect them at runtime and tests
    can monkey-patch them per-case."""
    log("driver loop caps: LOOP_MAX_STEPS / LOOP_MAX_USD / LOOP_CHECKPOINT_EVERY")
    from agentflow_computer_mcp.driver import loop as driver_loop

    for name in ("LOOP_MAX_STEPS", "LOOP_MAX_USD", "LOOP_CHECKPOINT_EVERY"):
        if not hasattr(driver_loop, name):
            fail(f"driver.loop missing constant: {name}")
    if not isinstance(driver_loop.LOOP_MAX_STEPS, int):
        fail("LOOP_MAX_STEPS must be int")
    if not isinstance(driver_loop.LOOP_MAX_USD, float):
        fail("LOOP_MAX_USD must be float")
    if not isinstance(driver_loop.LOOP_CHECKPOINT_EVERY, int):
        fail("LOOP_CHECKPOINT_EVERY must be int")
    log(
        f"  ok -- caps: steps={driver_loop.LOOP_MAX_STEPS}, "
        f"usd=${driver_loop.LOOP_MAX_USD}, checkpoint_every={driver_loop.LOOP_CHECKPOINT_EVERY}"
    )


def check_loop_checkpoint_abort() -> None:
    """Drive a fake run_task that hits a checkpoint returning on_track=false
    and assert the loop aborts cleanly (task_error emitted, no exception).

    We don't spin up a full DriverState/ToolExecutor — we stub the LLM
    transport at the post_llm_cancellable seam, which is what every
    code path in run_task funnels through.
    """
    log("driver loop checkpoint abort: mock LLM returns on_track=false")
    import threading
    from unittest.mock import patch

    from agentflow_computer_mcp.driver import loop as driver_loop
    from agentflow_computer_mcp.driver.state import DriverState

    # Force a low checkpoint cadence so we hit the reflection turn fast.
    # The minimum-step gate is 3 — pick CHECKPOINT_EVERY=3 to fire at step 3.
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HOME"] = tmp
        os.environ["USERPROFILE"] = tmp

        state = DriverState()
        state.current_task_id = "smoke-task-1"
        outbound: list[dict] = []
        state.outbound_publisher = lambda frame: outbound.append(frame)

        call_count = {"n": 0}

        def fake_post_llm(url, api_key, payload, abort_flag, timeout=180, poll_interval=0.2):  # noqa: ANN001, ARG001
            call_count["n"] += 1
            messages = payload.get("messages", [])
            last_user_text = ""
            if messages and messages[-1].get("role") == "user":
                content = messages[-1].get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        last_user_text = block.get("text", "")
                        break
            # Checkpoint probe? Return on_track=false.
            if "Сейчас сделано" in last_user_text or "потерялся" in last_user_text:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"on_track": false, "next_step": "", "abandon_reason": "lost in loop"}',
                        }
                    ],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
            # Normal turn: issue a benign tool call to bump the step counter.
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"tool_{call_count['n']}",
                        "name": "noop_tool",
                        "input": {},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 50, "output_tokens": 80},
            }

        class FakeExecutor:
            _af = None  # so af_present is False, skip af tools

            def execute(self, name, args):  # noqa: ANN001, ARG002
                return ("ok", None)

        # Force checkpoint to fire fast (every 3 steps, min steps is 3).
        with (
            patch.object(driver_loop, "LOOP_CHECKPOINT_EVERY", 3),
            patch.object(driver_loop, "LOOP_MAX_STEPS", 50),
            patch.object(driver_loop, "LOOP_MAX_USD", 999.0),
            patch.object(driver_loop, "post_llm_cancellable", fake_post_llm),
            # Force memory + budget into the temp DB. They auto-mkdir.
            patch(
                "agentflow_computer_mcp.autonomous.schema.DEFAULT_DB_PATH",
                Path(tmp) / "autonomous.db",
            ),
            patch(
                "agentflow_computer_mcp.autonomous.memory.DEFAULT_DB_PATH",
                Path(tmp) / "autonomous.db",
            ),
            patch(
                "agentflow_computer_mcp.autonomous.budget.DEFAULT_DB_PATH",
                Path(tmp) / "autonomous.db",
            ),
        ):
            # Run with a hard wall-clock guard so a bug doesn't hang the smoke.
            result_box: dict = {}

            def _runner():
                try:
                    result_box["value"] = driver_loop.run_task(
                        "smoke checkpoint task: do 5 things and then stop",
                        state,
                        FakeExecutor(),
                        api_key="af_live_smoketest",
                        max_iters=20,
                    )
                except Exception as exc:  # noqa: BLE001
                    result_box["error"] = repr(exc)

            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            t.join(timeout=30)
            if t.is_alive():
                fail("run_task did not return within 30s after checkpoint abort")

        if "error" in result_box:
            fail(f"run_task raised on checkpoint abort: {result_box['error']}")

        # Verify a task_error frame was emitted with our abandon reason.
        abort_frames = [f for f in outbound if f.get("type") == "task_error"]
        if not abort_frames:
            fail(f"no task_error frame emitted on abandon; outbound={outbound}")
        if not any("task.abandon" in (f.get("error") or "") for f in abort_frames):
            fail(
                f"task_error did not carry abandon reason; got: "
                f"{[f.get('error') for f in abort_frames]}"
            )

    log("  ok — loop aborted cleanly via task_error frame")


def main() -> None:
    log("starting smoke for installer/setup_gui.py")
    check_invite_roundtrip()
    check_auth_file_shape()
    check_daemon_entrypoint_imports()
    check_autonomous_skeleton()
    check_auto_updater()
    check_os_aware_tool_filter()
    check_os_aware_system_prompt()
    check_loop_caps_constants()
    check_loop_checkpoint_abort()
    log("ALL GREEN")


if __name__ == "__main__":
    main()
