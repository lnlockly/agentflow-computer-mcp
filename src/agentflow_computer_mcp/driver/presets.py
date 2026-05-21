"""Load preset task library from ``presets/desktop-tasks.yaml`` (or alt path).

The YAML format is intentionally trivial — a list of records with ``label`` + ``task``.
We avoid a YAML dep: this loader is a minimal parser for the constrained format we ship.
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_PRESETS_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "presets" / "desktop-tasks.yaml"
)


def _parse_simple_yaml(text: str) -> list[dict[str, str]]:
    """Parse a list of ``- label: ...`` / ``  task: |``-block entries. Trivial format only."""
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    pending_key: str | None = None
    block_indent = 0
    block_lines: list[str] = []

    def flush_block() -> None:
        nonlocal pending_key, block_lines
        if current is not None and pending_key is not None:
            current[pending_key] = "\n".join(block_lines).strip()
        pending_key = None
        block_lines = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if pending_key is not None:
            if line.strip() == "" or line.startswith(" " * block_indent):
                block_lines.append(line[block_indent:] if line.startswith(" " * block_indent) else line)
                continue
            flush_block()

        if not line.strip() or line.lstrip().startswith("#"):
            continue

        stripped = line.lstrip()
        if stripped.startswith("- "):
            if current is not None:
                items.append(current)
            current = {}
            rest = stripped[2:].strip()
            if rest:
                _consume_key_value(rest, current)
            continue
        if current is None:
            continue
        result = _consume_key_value(stripped, current)
        if result == "block":
            indent_match = len(line) - len(line.lstrip())
            pending_key = stripped.split(":", 1)[0].strip()
            block_indent = indent_match + 2
            block_lines = []

    flush_block()
    if current is not None:
        items.append(current)
    return [it for it in items if it.get("label") and it.get("task")]


def _consume_key_value(line: str, current: dict[str, str]) -> str | None:
    if ":" not in line:
        return None
    key, val = line.split(":", 1)
    key = key.strip()
    val = val.strip()
    if val == "|":
        return "block"
    if val.startswith('"') and val.endswith('"') and len(val) >= 2:
        val = val[1:-1]
    current[key] = val
    return None


def load_presets(path: Path | None = None) -> list[dict[str, str]]:
    target = path or DEFAULT_PRESETS_PATH
    if not target.exists():
        return []
    return _parse_simple_yaml(target.read_text(encoding="utf-8"))
