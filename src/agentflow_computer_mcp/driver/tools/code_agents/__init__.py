"""Swappable code-agent backends.

The hosted daemon spawns a code-editing CLI (aider today, anything else
tomorrow) to turn a user brief into a working project. This package
holds one file per backend and a tiny registry so the choice is made at
runtime from :envvar:`CODE_AGENT_BACKEND`.

Adding a new tool:

1. Drop ``foo.py`` here with a :class:`FooBackend(CodeAgentBackend)`.
2. Register it in :data:`_REGISTRY` below.
3. Set ``CODE_AGENT_BACKEND=foo`` in the daemon's env. No agent_brief
   change, no image rebuild beyond shipping the binary.

The default stays ``aider`` so an unset env var keeps the production
behaviour byte-for-byte identical to the pre-refactor flow.
"""

from __future__ import annotations

import os

from .aider import AiderBackend
from .base import CodeAgentBackend
from .cli_generic import GenericCLIBackend
from .goose import GooseBackend
from .opencode import OpenCodeBackend

# Single source of truth for backend selection. Keys are the values
# accepted by ``CODE_AGENT_BACKEND``. Add new backends here; ordering
# does not matter — the env-var lookup is exact-match.
_REGISTRY: dict[str, type[CodeAgentBackend]] = {
    "aider": AiderBackend,
    "opencode": OpenCodeBackend,
    "goose": GooseBackend,
    "cli": GenericCLIBackend,
}


def list_backends() -> list[str]:
    """Return the registered backend slugs (used by diagnostics)."""
    return sorted(_REGISTRY.keys())


def get_backend(slug: str | None = None) -> CodeAgentBackend:
    """Return a fresh backend instance for ``slug`` (env-driven by default).

    Lookup order:
    1. Explicit ``slug`` argument (overrides everything — used by tests).
    2. :envvar:`CODE_AGENT_BACKEND` env var.
    3. Default ``aider``.

    Raises
    ------
    ValueError
        When the requested slug is not in :data:`_REGISTRY`. The error
        message lists the valid slugs so the operator can fix the env
        var without grepping the codebase.
    """
    resolved = (slug or os.environ.get("CODE_AGENT_BACKEND") or "aider").strip().lower()
    cls = _REGISTRY.get(resolved)
    if cls is None:
        valid = ", ".join(sorted(_REGISTRY.keys()))
        raise ValueError(
            f"unknown code agent backend: {resolved!r} (valid: {valid}). "
            "Set CODE_AGENT_BACKEND to one of those or unset it for the "
            "default (aider)."
        )
    return cls()


__all__ = [
    "AiderBackend",
    "CodeAgentBackend",
    "GenericCLIBackend",
    "GooseBackend",
    "OpenCodeBackend",
    "get_backend",
    "list_backends",
]
