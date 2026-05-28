"""Goose backend — block/goose Rust-based code agent.

Goose (https://github.com/block/goose) is a Rust binary that talks
OpenAI-shaped HTTP directly via its own client. That sidesteps the
litellm payload-blocking issue that broke aider on the AF gateway for
some briefs (aider routes through Python litellm which the gateway
sometimes rejects with ``payload_blocked`` before the model ever sees
the request).

Owner verified locally that this invocation edits files end-to-end
through the AF gateway::

    OPENAI_API_KEY=$AF_API_KEY \\
    OPENAI_HOST=https://agentflow.website \\
    OPENAI_BASE_PATH=/llm/v1/chat/completions \\
    GOOSE_PROVIDER=openai \\
    GOOSE_MODEL=flow \\
    goose run --with-builtin developer --no-session -t "<brief>"

The argv shape matches that recipe verbatim. ``OPENAI_HOST`` +
``OPENAI_BASE_PATH`` is how goose targets a non-api.openai.com endpoint
— it is **not** the OpenAI shim that aider/opencode use.
"""

from __future__ import annotations

import shutil
import subprocess
from urllib.parse import urlparse

from .base import CodeAgentBackend


def _split_host_from_api_base(api_base: str) -> str:
    """Return the scheme+host root for ``OPENAI_HOST``.

    Two shapes show up in practice:

    * Production: ``https://agentflow.website/_agents`` — daemon receives
      the gateway base without ``/llm/v1`` suffix; we trim path entirely
      and keep ``https://agentflow.website`` so goose's
      ``OPENAI_BASE_PATH=/llm/v1/chat/completions`` lands on the right
      route.
    * Tests / legacy: ``https://x.com/llm/v1`` — split on the suffix so
      the assertion ``env["OPENAI_HOST"] == "https://x.com"`` holds.

    Falls back to the input string when parsing fails, so a malformed
    ``api_base`` produces a clear spawn-time error rather than a silent
    wrong host.
    """
    if not api_base:
        return ""
    if "/llm/v1" in api_base:
        return api_base.split("/llm/v1")[0]
    parsed = urlparse(api_base)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return api_base


class GooseBackend(CodeAgentBackend):
    """Spawns ``goose run --with-builtin developer --no-session -t "<brief>"``.

    ``--with-builtin developer`` loads goose's developer extension which
    ships the file-edit / shell tools — without it goose runs as a
    chat-only agent and never touches the workspace. ``--no-session``
    disables on-disk session resume so each project run starts clean.
    """

    slug = "goose"

    def __init__(self, binary: str = "goose") -> None:
        self.binary = binary

    def build_command(
        self,
        *,
        brief: str,
        project_dir: str,
        api_key: str,
        api_base: str,
        model: str = "flow",
    ) -> tuple[list[str], dict[str, str]]:
        env_overrides: dict[str, str] = {
            "GOOSE_PROVIDER": "openai",
            "GOOSE_MODEL": model,
            # Goose ships an analytics ping on each run; silence it so
            # egress restrictions in the coder pod don't surface as
            # warnings in the agent_log stream.
            "GOOSE_ANALYTICS_ENABLED": "false",
        }
        if api_key:
            env_overrides["OPENAI_API_KEY"] = api_key
            env_overrides["OPENAI_HOST"] = _split_host_from_api_base(api_base)
            env_overrides["OPENAI_BASE_PATH"] = "/llm/v1/chat/completions"

        argv = [
            self.binary,
            "run",
            "--with-builtin", "developer",
            "--no-session",
            "-t", brief,
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
