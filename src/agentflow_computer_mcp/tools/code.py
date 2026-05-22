"""IDE-style coding tools: read/write/edit/run/list with shared scope checks.

These wrap the lower-level ``fs`` and ``shell`` primitives but expose an
ergonomic, code-editor-shaped API the LLM can drive directly. They reuse
``check_path`` for the deny-list (``~/.ssh``, ``~/.config``, etc.) so secrets
never leak through the new surface.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import time
from pathlib import Path
from typing import Any

from ..config import Scope
from ..scope import check_path, check_shell
from . import fs as fs_tool
from . import shell as shell_tool

DEFAULT_IGNORE: tuple[str, ...] = (".git", "node_modules", "dist", ".venv", "__pycache__")
MAX_LIST_ENTRIES = 2000


def _shared_scope_check(path: str, scope: Scope, write: bool) -> Path:
    """Single entry point so deny-paths apply uniformly across all code_* tools."""
    return check_path(path, scope, write=write)


def read_file(path: str, scope: Scope, max_lines: int = 2000) -> dict[str, Any]:
    target = _shared_scope_check(path, scope, write=False)
    if not target.exists():
        raise FileNotFoundError(str(target))
    if not target.is_file():
        raise IsADirectoryError(str(target))

    with target.open("r", encoding="utf-8", errors="replace") as fp:
        lines = fp.readlines()

    total = len(lines)
    truncated = total > max_lines
    head = "".join(lines[:max_lines])
    return {
        "path": str(target),
        "line_count": total,
        "truncated": truncated,
        "content": head,
    }


def write_file(
    path: str,
    content: str,
    scope: Scope,
    mode: str = "replace",
) -> dict[str, Any]:
    if mode not in ("replace", "append"):
        raise ValueError(f"mode must be 'replace' or 'append', got {mode!r}")

    target = _shared_scope_check(path, scope, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    if mode == "append" and target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        target.write_text(existing + content, encoding="utf-8")
    else:
        target.write_text(content, encoding="utf-8")

    return {
        "path": str(target),
        "mode": mode,
        "size_bytes": target.stat().st_size,
    }


def edit_file(
    path: str,
    find: str,
    replace: str,
    scope: Scope,
    count: int | str = 1,
) -> dict[str, Any]:
    target = _shared_scope_check(path, scope, write=True)
    if not target.exists():
        raise FileNotFoundError(str(target))

    original = target.read_text(encoding="utf-8", errors="replace")
    occurrences = original.count(find)
    if occurrences == 0:
        raise ValueError(f"find pattern not present in {target}")

    if count == "all":
        new_content = original.replace(find, replace)
        replaced = occurrences
    else:
        try:
            n = int(count)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"count must be int or 'all', got {count!r}") from exc
        if n < 1:
            raise ValueError("count must be >= 1 or 'all'")
        if occurrences > n:
            raise ValueError(
                f"find pattern appears {occurrences} times, exceeds count={n}; "
                "pass count='all' or add more context to find"
            )
        new_content = original.replace(find, replace, n)
        replaced = occurrences

    target.write_text(new_content, encoding="utf-8")
    return {
        "path": str(target),
        "replaced": replaced,
        "size_bytes": target.stat().st_size,
    }


async def run_command(
    command: str,
    scope: Scope,
    cwd: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    check_shell(command, scope)

    workdir = cwd or str(Path.home())
    workdir_path = _shared_scope_check(workdir, scope, write=False)
    if not workdir_path.is_dir():
        raise NotADirectoryError(str(workdir_path))

    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(workdir_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    duration_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "cwd": str(workdir_path),
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": stdout[: shell_tool.MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stderr": stderr[: shell_tool.MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "duration_ms": duration_ms,
    }


def list_dir(
    path: str,
    scope: Scope,
    depth: int = 1,
    ignore_globs: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    target = _shared_scope_check(path, scope, write=False)
    if not target.is_dir():
        raise NotADirectoryError(str(target))

    patterns = tuple(ignore_globs) if ignore_globs is not None else DEFAULT_IGNORE
    entries: list[dict[str, Any]] = []

    def _ignored(name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in patterns)

    base_depth = len(target.parts)
    for dirpath, dirnames, filenames in os.walk(target):
        rel_depth = len(Path(dirpath).parts) - base_depth
        if rel_depth >= depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not _ignored(d)]

        for name in filenames:
            if _ignored(name):
                continue
            full = Path(dirpath) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            entries.append(
                {
                    "path": str(full),
                    "is_dir": False,
                    "size_bytes": stat.st_size,
                }
            )
            if len(entries) >= MAX_LIST_ENTRIES:
                return {"path": str(target), "entries": entries, "truncated": True}

        for name in dirnames:
            full = Path(dirpath) / name
            entries.append({"path": str(full), "is_dir": True, "size_bytes": None})
            if len(entries) >= MAX_LIST_ENTRIES:
                return {"path": str(target), "entries": entries, "truncated": True}

    return {"path": str(target), "entries": entries, "truncated": False}


# Convenience for tests/callers that already have an event loop.
__all__ = [
    "DEFAULT_IGNORE",
    "edit_file",
    "list_dir",
    "read_file",
    "run_command",
    "write_file",
]


# Mirror MAX_OUTPUT_BYTES so tests can reference it without going through shell.
MAX_OUTPUT_BYTES = shell_tool.MAX_OUTPUT_BYTES
