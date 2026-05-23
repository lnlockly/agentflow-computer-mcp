"""Slot discovery + legacy migration.

On daemon start we scan `~/.agentflow/agents/*/scope.toml`. If the
directory is empty we migrate a legacy single-agent install (a top-level
`auth.json` + `computer-scope.toml`) into `agents/default/` and drop a
`.migrated` marker so we never migrate twice.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ..config import AGENTFLOW_DIR
from .slot import AgentSlot

log = logging.getLogger(__name__)

AGENTS_DIR_NAME = "agents"
LEGACY_AUTH_FILE = "auth.json"
LEGACY_SCOPE_FILE = "computer-scope.toml"
MIGRATED_MARKER = ".migrated"


def agents_root(base: Path | None = None) -> Path:
    return (base or AGENTFLOW_DIR) / AGENTS_DIR_NAME


def migrate_legacy(base: Path | None = None) -> Path | None:
    """If `base/auth.json` exists and `base/agents/default/` does not, move
    the legacy files into `agents/default/`. Returns the new dir or None.
    """
    base = base or AGENTFLOW_DIR
    default_dir = agents_root(base) / "default"
    marker = default_dir / MIGRATED_MARKER
    if marker.exists():
        return None
    legacy_auth = base / LEGACY_AUTH_FILE
    legacy_scope = base / LEGACY_SCOPE_FILE
    if not legacy_auth.exists() and not legacy_scope.exists():
        return None

    default_dir.mkdir(parents=True, exist_ok=True)
    if legacy_auth.exists() and not (default_dir / LEGACY_AUTH_FILE).exists():
        # We copy (not move) — top-level auth.json is still read by the
        # legacy code paths (ws bridge, etc.) until those paths fully cut over.
        shutil.copy2(legacy_auth, default_dir / LEGACY_AUTH_FILE)
    if legacy_scope.exists() and not (default_dir / "scope.toml").exists():
        shutil.copy2(legacy_scope, default_dir / "scope.toml")
    marker.write_text("migrated\n", encoding="utf-8")
    log.info("[agent-bootstrap] legacy migrate → %s", default_dir)
    return default_dir


def is_multi_agent_enabled() -> bool:
    return os.environ.get("AGENTFLOW_MULTI_AGENT", "").lower() in {"1", "true", "yes"}


def discover_slots(base: Path | None = None) -> list[AgentSlot]:
    """Return one AgentSlot per `agents/<id>/scope.toml`.

    Always guarantees a `default` slot exists (creates the dir if not).
    When multi-agent is disabled via env, only the default slot is returned
    even if more dirs exist on disk — back-compat for users who created
    extra dirs but didn't opt in.
    """
    base = base or AGENTFLOW_DIR
    migrate_legacy(base)
    root = agents_root(base)
    root.mkdir(parents=True, exist_ok=True)
    default_dir = root / "default"
    default_dir.mkdir(parents=True, exist_ok=True)

    if not is_multi_agent_enabled():
        return [_slot_from_dir(default_dir)]

    slots: list[AgentSlot] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        slots.append(_slot_from_dir(child))
    if not any(s.id == "default" for s in slots):
        slots.insert(0, _slot_from_dir(default_dir))
    return slots


def _slot_from_dir(path: Path) -> AgentSlot:
    persona_file = path / "persona.txt"
    scope_file = path / "scope.toml"
    return AgentSlot(
        id=path.name,
        name=path.name,
        persona=persona_file.read_text(encoding="utf-8").strip() if persona_file.exists() else "",
        scope_path=str(scope_file) if scope_file.exists() else "",
    )


def create_slot_dir(
    base: Path | None,
    name: str,
    persona: str = "",
    scope_path: str | None = None,
) -> Path:
    """Materialize a new slot dir under agents/<name>/.

    `scope_path` is copied into the slot dir as `scope.toml` when provided.
    """
    base = base or AGENTFLOW_DIR
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_").strip() or "agent"
    slot_dir = agents_root(base) / safe_name
    slot_dir.mkdir(parents=True, exist_ok=True)
    if persona:
        (slot_dir / "persona.txt").write_text(persona, encoding="utf-8")
    if scope_path:
        src = Path(scope_path).expanduser()
        if src.exists():
            shutil.copy2(src, slot_dir / "scope.toml")
    (slot_dir / "logs").mkdir(exist_ok=True)
    return slot_dir
