"""Bootstrap: legacy migration + slot discovery + multi-agent gating."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentflow_computer_mcp.agents import bootstrap


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path


def test_legacy_migration_copies_files(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (base / "auth.json").write_text('{"api_key": "key"}', encoding="utf-8")
    (base / "computer-scope.toml").write_text("budget_usd = 1.0\n", encoding="utf-8")
    out = bootstrap.migrate_legacy(base)
    assert out is not None
    assert (out / "auth.json").exists()
    assert (out / "scope.toml").exists()
    assert (out / ".migrated").exists()
    # Re-run is a no-op.
    assert bootstrap.migrate_legacy(base) is None


def test_legacy_migration_skipped_when_nothing_to_move(base: Path) -> None:
    assert bootstrap.migrate_legacy(base) is None


def test_discover_default_only_when_disabled(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTFLOW_MULTI_AGENT", raising=False)
    slots = bootstrap.discover_slots(base)
    assert [s.id for s in slots] == ["default"]


def test_discover_returns_all_when_enabled(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTFLOW_MULTI_AGENT", "1")
    bootstrap.create_slot_dir(base, "trader", persona="trade")
    bootstrap.create_slot_dir(base, "researcher", persona="research")
    slots = bootstrap.discover_slots(base)
    ids = {s.id for s in slots}
    assert {"default", "trader", "researcher"} <= ids


def test_create_slot_dir_writes_persona(base: Path) -> None:
    out = bootstrap.create_slot_dir(base, "trader", persona="be a careful trader")
    assert (out / "persona.txt").read_text(encoding="utf-8") == "be a careful trader"
    assert (out / "logs").is_dir()


def test_create_slot_dir_sanitizes_name(base: Path) -> None:
    out = bootstrap.create_slot_dir(base, "../bad name!", persona="")
    assert out.name == "badname"
