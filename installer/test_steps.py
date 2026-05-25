"""Tests for the install-wizard step contract.

Run: `pytest installer/test_steps.py -v`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from installer.steps import (
    SUPPORTED_SCHEMA_VERSION,
    StepRunner,
    StepsManifestError,
    UnknownStepError,
    load_steps,
)


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "steps.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_load_canonical_manifest_has_all_expected_steps():
    """The shipped installer/steps.json must list the 12 canonical names."""
    steps = load_steps()
    names = [s.name for s in steps]
    assert names == [
        "prepare_workspace",
        "verify_token",
        "request_permissions",
        "install_daemon_binary",
        "write_auth_json",
        "autostart_register",
        "install_opencode_cli",
        "install_pencil_mcp",
        "sync_skill_packs",
        "register_mcp_servers",
        "launch_daemon",
        "verify_install",
    ]


def test_load_filters_by_surface_hosted_skips_os_only_steps():
    hosted = [s.name for s in load_steps(surface="hosted")]
    assert "install_daemon_binary" not in hosted
    assert "request_permissions" not in hosted
    assert "autostart_register" not in hosted
    # Cross-surface ones stay
    assert "prepare_workspace" in hosted
    assert "verify_install" in hosted


def test_load_rejects_unsupported_schema_version(tmp_path: Path):
    path = _write_manifest(tmp_path, {"version": 99, "steps": []})
    with pytest.raises(StepsManifestError, match="unsupported steps.json schema version"):
        load_steps(path=path)


def test_load_rejects_missing_file(tmp_path: Path):
    with pytest.raises(StepsManifestError, match="not found"):
        load_steps(path=tmp_path / "nope.json")


def test_load_rejects_missing_required_field(tmp_path: Path):
    path = _write_manifest(
        tmp_path,
        {
            "version": SUPPORTED_SCHEMA_VERSION,
            "steps": [{"name": "x", "label_ru": "X", "label_en": "X", "surfaces": ["win"], "required": True}],
        },
    )
    with pytest.raises(StepsManifestError, match="missing required field"):
        load_steps(path=path)


def test_load_rejects_invalid_status(tmp_path: Path):
    path = _write_manifest(
        tmp_path,
        {
            "version": SUPPORTED_SCHEMA_VERSION,
            "steps": [
                {
                    "name": "x",
                    "label_ru": "X",
                    "label_en": "X",
                    "surfaces": ["win"],
                    "required": True,
                    "status": "weird",
                }
            ],
        },
    )
    with pytest.raises(StepsManifestError, match="invalid status"):
        load_steps(path=path)


# ---------------------------------------------------------------------------
# Runner — happy path + skip semantics
# ---------------------------------------------------------------------------


def _make_progress_recorder():
    events: list[tuple[str, str, str]] = []

    def record(name: str, status: str, detail: str) -> None:
        events.append((name, status, detail))

    return events, record


def test_runner_skips_planned_steps_without_calling_fn():
    steps = load_steps(surface="hosted")
    events, record = _make_progress_recorder()
    runner = StepRunner(steps=steps, progress=record, surface="hosted")
    called: list[str] = []

    # Register a callback for a planned step — it must NOT be invoked.
    runner.register("install_pencil_mcp", lambda ctx: called.append("pencil"))
    # Register required steps so the runner doesn't error-stop on them.
    for name in (
        "prepare_workspace",
        "verify_token",
        "write_auth_json",
        "install_opencode_cli",
        "launch_daemon",
        "verify_install",
    ):
        runner.register(name, lambda ctx, _n=name: None)

    runner.run()

    assert called == [], "planned step fn was invoked — runner contract violated"
    statuses = [(n, s) for n, s, _ in events]
    assert ("install_pencil_mcp", "skipped_planned") in statuses


def test_runner_marks_missing_callback_for_required_step_as_error():
    steps = load_steps(surface="hosted")
    events, record = _make_progress_recorder()
    runner = StepRunner(steps=steps, progress=record, surface="hosted")
    # Deliberately don't register prepare_workspace (required, real).
    runner.run()
    # First emitted runtime event must be the missing-callback error.
    first_runtime = next((e for e in events if e[1] not in ("skipped_surface",)), None)
    assert first_runtime is not None
    assert first_runtime[0] == "prepare_workspace"
    assert first_runtime[1] == "error"


def test_runner_stops_after_required_step_raises():
    steps = load_steps(surface="hosted")
    events, record = _make_progress_recorder()
    runner = StepRunner(steps=steps, progress=record, surface="hosted")

    runner.register("prepare_workspace", lambda ctx: None)

    def boom(_ctx):
        raise RuntimeError("kaboom")

    runner.register("verify_token", boom)
    # Register downstream too — they should NOT execute because verify_token is required.
    later_calls: list[str] = []
    for n in (
        "write_auth_json",
        "install_opencode_cli",
        "launch_daemon",
        "verify_install",
    ):
        runner.register(n, lambda ctx, _n=n: later_calls.append(_n))

    runner.run()

    statuses = {n: s for n, s, _ in events}
    assert statuses["verify_token"] == "error"
    assert later_calls == [], "downstream steps ran after required-step error"


def test_runner_continues_after_optional_step_error():
    """An error in a non-required step must not block later steps."""
    steps = load_steps(surface="win")
    events, record = _make_progress_recorder()
    runner = StepRunner(steps=steps, progress=record, surface="win")

    runner.register("prepare_workspace", lambda ctx: None)
    runner.register("verify_token", lambda ctx: None)

    def boom(_ctx):
        raise RuntimeError("optional fail")

    runner.register("request_permissions", boom)  # required=False
    runner.register("install_daemon_binary", lambda ctx: None)
    runner.register("write_auth_json", lambda ctx: None)
    runner.register("autostart_register", lambda ctx: None)
    runner.register("install_opencode_cli", lambda ctx: None)
    runner.register("launch_daemon", lambda ctx: None)
    runner.register("verify_install", lambda ctx: None)

    runner.run()

    statuses = {n: s for n, s, _ in events}
    assert statuses["request_permissions"] == "error"
    assert statuses["install_daemon_binary"] == "ok"
    assert statuses["verify_install"] == "ok"


def test_runner_register_unknown_step_raises():
    runner = StepRunner(steps=load_steps(surface="win"), surface="win")
    with pytest.raises(UnknownStepError):
        runner.register("nonexistent_step", lambda ctx: None)


def test_runner_surface_filter_marks_off_surface_as_skipped():
    """When manifest contains a step that doesn't apply to surface, runner emits skipped_surface."""
    # Build a runner with steps from the full manifest (no filter) but surface=hosted.
    steps = load_steps(surface=None)
    events, record = _make_progress_recorder()
    runner = StepRunner(steps=steps, progress=record, surface="hosted")
    # Register only the hosted-relevant required ones.
    for n in (
        "prepare_workspace",
        "verify_token",
        "write_auth_json",
        "install_opencode_cli",
        "launch_daemon",
        "verify_install",
    ):
        runner.register(n, lambda ctx, _n=n: None)

    runner.run()
    statuses = {n: s for n, s, _ in events}
    # install_daemon_binary is win/mac/linux only — must be skipped_surface on hosted.
    assert statuses["install_daemon_binary"] == "skipped_surface"
