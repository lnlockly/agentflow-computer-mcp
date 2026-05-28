"""Generic CLI backend driven by environment variables.

Lets the owner wire a new tool (Continue, Goose, Cline, a Bash wrapper
around anything) without writing Python. Two env vars drive it:

* :envvar:`CODE_AGENT_CLI_CMD` — required template string. Tokens
  ``{brief}``, ``{project_dir}``, ``{api_key}``, ``{api_base}``,
  ``{model}`` are substituted at build time. Anything else is passed
  through literally.
* :envvar:`CODE_AGENT_CLI_ENV` — optional ``KEY=VAL;KEY=VAL`` list of
  extra env overrides (semicolon-delimited because ``=`` is already
  taken). Tokens above are substituted in the values too.

Example for a hypothetical Continue CLI::

    CODE_AGENT_BACKEND=cli
    CODE_AGENT_CLI_CMD="continue --root {project_dir} --model {model} --prompt '{brief}'"
    CODE_AGENT_CLI_ENV='CONTINUE_API_KEY={api_key};CONTINUE_BASE={api_base}/llm/v1'

The template is split with :func:`shlex.split` so the ``{brief}`` slot
**must** be wrapped in quotes — otherwise the brief's whitespace gets
split into separate argv slots. Missing tokens raise ``ValueError`` at
build time so an unset env produces a clear error in the spawn result
instead of a silent partial argv.
"""

from __future__ import annotations

import os
import shlex
import shutil

from .base import CodeAgentBackend


def _substitute(template: str, **slots: str) -> str:
    """Replace ``{name}`` tokens with values from ``slots``.

    Uses :meth:`str.format_map` with a missing-key guard so an unknown
    token raises a precise error rather than ``KeyError``-on-deep-frames.
    """
    try:
        return template.format_map(_SlotMap(slots))
    except _MissingSlot as exc:
        raise ValueError(f"unknown slot in CODE_AGENT_CLI template: {{{exc.name}}}") from None


class _MissingSlot(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class _SlotMap(dict):
    def __missing__(self, key: str):  # pragma: no cover - exercised via _substitute
        raise _MissingSlot(key)


class GenericCLIBackend(CodeAgentBackend):
    """Configurable shell-style backend.

    Reads :envvar:`CODE_AGENT_CLI_CMD` on every ``build_command`` so
    operators can flip behaviour without restarting the daemon (the
    backend is constructed per-spawn).
    """

    slug = "cli"

    def build_command(
        self,
        *,
        brief: str,
        project_dir: str,
        api_key: str,
        api_base: str,
        model: str = "openai/flow",
    ) -> tuple[list[str], dict[str, str]]:
        template = os.environ.get("CODE_AGENT_CLI_CMD", "").strip()
        if not template:
            raise ValueError(
                "CODE_AGENT_BACKEND=cli requires CODE_AGENT_CLI_CMD env var "
                "(template string with {brief}/{project_dir}/{api_key}/"
                "{api_base}/{model} tokens)"
            )
        slots = {
            "brief": brief,
            "project_dir": project_dir,
            "api_key": api_key,
            "api_base": api_base,
            "model": model,
        }
        rendered = _substitute(template, **slots)
        # shlex.split handles quoted args with spaces — without it the
        # brief itself would be word-split into many argv slots.
        argv = shlex.split(rendered)
        if not argv:
            raise ValueError("CODE_AGENT_CLI_CMD rendered to empty argv")

        env_overrides: dict[str, str] = {}
        env_template = os.environ.get("CODE_AGENT_CLI_ENV", "").strip()
        if env_template:
            for pair in env_template.split(";"):
                pair = pair.strip()
                if not pair or "=" not in pair:
                    continue
                key, _, val = pair.partition("=")
                env_overrides[key.strip()] = _substitute(val, **slots)
        return argv, env_overrides

    def healthcheck(self) -> bool:
        template = os.environ.get("CODE_AGENT_CLI_CMD", "").strip()
        if not template:
            return False
        # First token is the binary; substitute dummy slots so the lookup
        # works even when the real values aren't available at boot.
        try:
            rendered = _substitute(
                template,
                brief="",
                project_dir="",
                api_key="",
                api_base="",
                model="",
            )
        except ValueError:
            return False
        argv = shlex.split(rendered)
        if not argv:
            return False
        return shutil.which(argv[0]) is not None
