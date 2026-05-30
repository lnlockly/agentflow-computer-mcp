from __future__ import annotations

from pathlib import Path

from .config import HARD_DENY_PATHS, SAFE_BASELINE_PROGRAMS, Scope


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
    head = cmd.strip().split()
    if not head:
        raise ScopeDenied("empty command")

    program = head[0]
    # The safe baseline (read-only diagnostics + common dev toolchain) is
    # always allowed so a device never blocks `uname`, `ls`, `cat`, etc. —
    # even when its stored scope carries an empty or narrow shell_whitelist.
    # The configurable shell_whitelist EXTENDS the baseline for anything
    # outside it (destructive or host-specific programs the owner opts in to).
    if program not in SAFE_BASELINE_PROGRAMS and program not in scope.shell_whitelist:
        if not scope.shell_whitelist:
            raise ScopeDenied(
                f"command not allowed: {program} (not in safe baseline and "
                "shell_whitelist is empty)"
            )
        raise ScopeDenied(f"command not in shell_whitelist: {program}")

    # Hard reject of recursive deletes. Hosted whitelists routinely
    # include `rm` so the agent can clean its own temp files, but a
    # mis-quoted prompt that ends with `rm -rf /` would otherwise nuke
    # the daemon's home directory. The whitelist is for programs, not
    # for arguments — this is the one argument-level guard.
    if program == "rm":
        for arg in head[1:]:
            if not arg.startswith("-"):
                continue
            if arg in ("-r", "-R", "-rf", "-fr", "-rfv", "-vrf"):
                raise ScopeDenied("rm with recursive flag is denied")
            if arg == "--recursive" or arg.startswith("--recursive"):
                raise ScopeDenied("rm with --recursive is denied")
            # Compact GNU-style cluster like `-rfv` or `-rfi` — refuse if
            # `r` or `R` is anywhere inside the cluster (and it's not a
            # long option).
            if not arg.startswith("--") and ("r" in arg[1:] or "R" in arg[1:]):
                raise ScopeDenied("rm with recursive flag is denied")
    return cmd


def requires_confirm(tool_name: str, scope: Scope) -> bool:
    return tool_name in scope.confirm_before
