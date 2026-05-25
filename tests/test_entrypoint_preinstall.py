"""Sanity tests for the preinstall wizard block inside docker/entrypoint.sh.

We can't run the full entrypoint (it execs agentflow-desktop, needs Xvfb,
sudo, apt, the ghcr image). But we can:
  1. Parse the file with `bash -n` to catch syntax breakage.
  2. Verify the case switch is exhaustive over PREINSTALL_TOOLS.
  3. Verify the reporting helper hits the right backend route + header.

Run: pytest tests/test_entrypoint_preinstall.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"

# Mirror of agentflow-agents/src/routes/me-hosted-devices.ts PREINSTALL_TOOLS.
# This list MUST stay in sync — adding a slug to the backend without a case
# branch here means the pod accepts the env var and silently does nothing.
KNOWN_TOOLS = ["opencode", "codex", "nodejs", "python_ds", "ffmpeg"]


def test_entrypoint_script_parses() -> None:
    """bash -n catches unbalanced quotes / heredocs / case blocks."""
    result = subprocess.run(
        ["bash", "-n", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_every_tool_has_a_case_branch() -> None:
    """One case label per supported slug — drift here = a silent no-op pod."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    for tool in KNOWN_TOOLS:
        assert (
            f"\n    {tool})" in text
        ), f"missing case branch for preinstall tool '{tool}' in entrypoint.sh"


def test_preinstall_event_posts_to_internal_route() -> None:
    """The reporting helper must hit /internal/hosted-devices/:id/preinstall-event."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "/_agents/internal/hosted-devices/" in text
    assert "/preinstall-event" in text
    assert "x-agentflow-secret:" in text


def test_preinstall_skipped_when_env_unset() -> None:
    """No AF_PREINSTALL → the for-loop block never enters."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'if [[ -n "${AF_PREINSTALL:-}" ]]; then' in text


def test_preinstall_event_skips_when_secret_missing() -> None:
    """preinstall_event returns 0 silently if AF_HOSTED_DEVICE_ID or
    AF_INTERNAL_SECRET aren't both set — selftest builds don't have them.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert (
        'if [[ -z "${AF_HOSTED_DEVICE_ID:-}" || -z "${AF_INTERNAL_SECRET:-}" ]]; then'
        in text
    )
