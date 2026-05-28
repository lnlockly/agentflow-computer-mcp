"""Aider backend — current production behaviour.

Argv + env shape mirror the ``_default_spawn_aider`` body that lived in
:mod:`agent_brief` before the refactor. The byte-for-byte parity is
load-bearing: ``tests/test_agent_dev_brief.py`` pins the exact wrapper
text, ``--yes-always`` / ``--no-pretty`` / ``--no-git`` flags, and the
``OPENAI_API_BASE`` + ``OPENAI_API_KEY`` env layering. Changing any of
those moves regressions out of this file and into the call site.

Why aider stays the default:

* ``--message`` mode is one-shot non-interactive (no in-loop prompts).
* ``--yes-always`` clears every confirmation prompt — the pod boundary
  already sandboxes the agent, in-loop prompts hang the spawn forever.
* ``--no-pretty`` keeps stdout line-by-line so the log tailer can
  stream cleanly to ``/agent-log``.
"""

from __future__ import annotations

import shutil
import subprocess

from .base import CodeAgentBackend


class AiderBackend(CodeAgentBackend):
    """Default backend. Spawns ``aider --message "<brief>"``.

    The binary name defaults to ``aider`` — overridable via
    :envvar:`CODE_AGENT_AIDER_BIN` for image variants that ship aider at
    a custom path (eg ``/usr/local/bin/aider`` in the hosted daemon image).
    """

    slug = "aider"

    def __init__(self, binary: str = "aider") -> None:
        self.binary = binary

    def build_command(
        self,
        *,
        brief: str,
        project_dir: str,
        api_key: str,
        api_base: str,
        model: str = "openai/flow",
    ) -> tuple[list[str], dict[str, str]]:
        # Aider treats ``openai/<model>`` as OpenAI-shaped and picks up
        # base + key from OPENAI_API_BASE + OPENAI_API_KEY env. The
        # project-local ``.aider.conf.yml`` would also work but env vars
        # keep gateway credentials out of the user's repo.
        env_overrides: dict[str, str] = {
            "AIDER_ANALYTICS": "false",
            "AIDER_CHECK_UPDATE": "false",
        }
        if api_key:
            env_overrides["OPENAI_API_BASE"] = api_base.rstrip("/") + "/llm/v1"
            env_overrides["OPENAI_API_KEY"] = api_key

        argv = [
            self.binary,
            "--yes-always",
            "--no-pretty",
            "--no-auto-commits",
            "--no-stream",
            "--no-git",
            "--no-show-model-warnings",
            "--model", model,
            "--edit-format", "diff",
            "--map-tokens", "2048",
            "--map-refresh", "auto",
            "--message", brief,
        ]
        return argv, env_overrides

    def healthcheck(self) -> bool:
        if not shutil.which(self.binary):
            return False
        try:
            proc = subprocess.run(  # noqa: S603 — fixed binary from config
                [self.binary, "--version"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0
