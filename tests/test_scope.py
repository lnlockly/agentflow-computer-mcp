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


def test_safe_baseline_allows_basics_with_empty_scope() -> None:
    # The friendly cabinet permission toggles never write shell_whitelist,
    # so a device can legitimately carry an empty whitelist. Read-only
    # basics must still run — this is the `uname -a` QA-failure fix.
    scope_empty = Scope(shell_whitelist=())
    assert check_shell("uname -a", scope_empty) == "uname -a"
    assert check_shell("ls -la", scope_empty) == "ls -la"
    assert check_shell("cat /etc/hosts", scope_empty) == "cat /etc/hosts"


def test_safe_baseline_still_gates_destructive_with_empty_scope() -> None:
    scope_empty = Scope(shell_whitelist=())
    # Destructive programs are NOT in the baseline; an empty whitelist gates.
    for cmd in ("dd if=/dev/zero of=/dev/sda", "mkfs.ext4 /dev/sda1", "shutdown -h now"):
        with pytest.raises(ScopeDenied):
            check_shell(cmd, scope_empty)


def test_shell_whitelist_extends_baseline() -> None:
    # A non-baseline program only runs when the owner opts in via the
    # configurable shell_whitelist; the baseline keeps working alongside it.
    scope = Scope(shell_whitelist=("kubectl",))
    assert check_shell("kubectl get pods", scope) == "kubectl get pods"
    assert check_shell("uname -a", scope) == "uname -a"  # baseline still on
    with pytest.raises(ScopeDenied):
        check_shell("helm install x", scope)  # not baseline, not whitelisted


def test_shell_whitelist_enforced() -> None:
    scope_ls = Scope(shell_whitelist=("ls", "pwd"))
    assert check_shell("ls -la", scope_ls) == "ls -la"
    # `rm` is destructive — not in the baseline — so a recursive rm is gated
    # even though the whitelist here doesn't list it.
    with pytest.raises(ScopeDenied):
        check_shell("rm -rf /", scope_ls)


def test_requires_confirm_defaults() -> None:
    scope = Scope()
    assert requires_confirm("computer.fs.write", scope) is True
    assert requires_confirm("computer.shell.exec", scope) is True
    assert requires_confirm("computer.screen.capture", scope) is False
