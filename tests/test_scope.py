from __future__ import annotations

from pathlib import Path

import pytest

from agentflow_computer_mcp.config import HARD_DENY_PATHS, Scope
from agentflow_computer_mcp.scope import ScopeDenied, check_path, check_shell, requires_confirm


def test_hard_deny_overrides_user_allow(tmp_path: Path) -> None:
    home = Path.home()
    scope = Scope(
        allow_paths=(str(home),),
        deny_paths=(),
    )
    with pytest.raises(ScopeDenied) as excinfo:
        check_path(str(home / ".ssh" / "id_rsa"), scope)
    assert "hard-coded fallback" in str(excinfo.value)


def test_hard_deny_covers_all_required_paths() -> None:
    scope = Scope(allow_paths=(str(Path.home()),))
    for p in HARD_DENY_PATHS:
        with pytest.raises(ScopeDenied):
            check_path(str(Path(p).expanduser() / "nested" / "file"), scope)


def test_allow_paths_required_when_set(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = check_path(str(tmp_path / "ok.txt"), scope)
    assert target.is_relative_to(tmp_path)

    with pytest.raises(ScopeDenied):
        check_path("/tmp/elsewhere.txt", scope)


def test_write_without_allow_paths_denied(tmp_path: Path) -> None:
    scope = Scope(allow_paths=())
    with pytest.raises(ScopeDenied):
        check_path(str(tmp_path / "x.txt"), scope, write=True)


def test_shell_whitelist_enforced() -> None:
    scope_empty = Scope(shell_whitelist=())
    with pytest.raises(ScopeDenied):
        check_shell("ls -la", scope_empty)

    scope_ls = Scope(shell_whitelist=("ls", "pwd"))
    assert check_shell("ls -la", scope_ls) == "ls -la"
    with pytest.raises(ScopeDenied):
        check_shell("rm -rf /", scope_ls)


def test_requires_confirm_defaults() -> None:
    scope = Scope()
    assert requires_confirm("computer.fs.write", scope) is True
    assert requires_confirm("computer.shell.exec", scope) is True
    assert requires_confirm("computer.screen.capture", scope) is False
