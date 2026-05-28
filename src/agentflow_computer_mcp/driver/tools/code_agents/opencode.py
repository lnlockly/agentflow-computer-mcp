"""OpenCode backend — legacy fallback.

Used before PR #123 replaced opencode with aider after opencode's
built-in iteration cap stalled multi-step briefs mid-todo-list. Kept
around so the owner can flip back by setting
``CODE_AGENT_BACKEND=opencode`` if aider hits a gateway-side regression.

The argv is the minimal shape that worked in prod before the swap:
``opencode run <brief>``. ``OPENAI_API_BASE`` + ``OPENAI_API_KEY`` env
override identical to aider — opencode also speaks the OpenAI shim.
"""

from __future__ import annotations

import shutil
import subprocess

from .base import CodeAgentBackend


class OpenCodeBackend(CodeAgentBackend):
    """Spawns ``opencode run "<brief>"`` in the project workspace."""

    slug = "opencode"

    def __init__(self, binary: str = "opencode") -> None:
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
        env_overrides: dict[str, str] = {}
        if api_key:
            env_overrides["OPENAI_API_BASE"] = api_base.rstrip("/") + "/llm/v1"
            env_overrides["OPENAI_API_KEY"] = api_key
        # opencode picks the model from OPENCODE_MODEL when set — letting
        # the alias flow through env keeps the argv stable across model
        # changes.
        env_overrides["OPENCODE_MODEL"] = model

        argv = [self.binary, "run", brief]
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
