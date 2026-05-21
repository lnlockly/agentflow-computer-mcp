from __future__ import annotations

from pathlib import Path

from .config import HARD_DENY_PATHS, Scope


class ScopeDenied(Exception):
    pass


def _expand(p: str) -> Path:
    return Path(p).expanduser().resolve(strict=False)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def check_path(path: str, scope: Scope, write: bool = False) -> Path:
    target = _expand(path)

    for deny in HARD_DENY_PATHS:
        deny_resolved = _expand(deny)
        if target == deny_resolved or _is_within(target, deny_resolved):
            raise ScopeDenied(f"path denied by hard-coded fallback: {deny}")

    for deny in scope.deny_paths:
        deny_resolved = _expand(deny)
        if target == deny_resolved or _is_within(target, deny_resolved):
            raise ScopeDenied(f"path denied by scope: {deny}")

    if scope.allow_paths:
        for allow in scope.allow_paths:
            allow_resolved = _expand(allow)
            if target == allow_resolved or _is_within(target, allow_resolved):
                return target
        raise ScopeDenied(f"path not in allow_paths: {target}")

    if write and not scope.allow_paths:
        raise ScopeDenied("fs.write requires explicit allow_paths in scope")

    return target


def check_shell(cmd: str, scope: Scope) -> str:
    if not scope.shell_whitelist:
        raise ScopeDenied("shell_whitelist is empty; shell.exec disabled")

    head = cmd.strip().split()
    if not head:
        raise ScopeDenied("empty command")

    program = head[0]
    if program not in scope.shell_whitelist:
        raise ScopeDenied(f"command not in shell_whitelist: {program}")
    return cmd


def requires_confirm(tool_name: str, scope: Scope) -> bool:
    return tool_name in scope.confirm_before
