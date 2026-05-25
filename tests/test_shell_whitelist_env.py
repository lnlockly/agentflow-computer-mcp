"""
Unit tests for the env-driven shell whitelist.

Bug C: hosted daemons started with an empty `Scope.shell_whitelist`,
which blocked every `code_run_command` invocation with
`shell_whitelist is empty; shell.exec disabled` and aborted autonomous
sessions as COMPLETION_BLOCKED. The fix wires `AF_SHELL_WHITELIST` from
the pod env into the loaded Scope and adds an argument-level guard for
recursive `rm`.

These tests pin three things:
  1. The env parser accepts comma-, newline-, and whitespace-separated
     formats and ignores blanks + `#` comments.
  2. `load_scope` reads the env var both when the TOML file is absent
     and when the file exists with extra entries (env wins, file
     extends).
  3. `check_shell` rejects every shape of `rm -r` even though `rm` is
     itself in the whitelist (program-level allow + argument-level
     guard).
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from agentflow_computer_mcp.config import (
    SHELL_WHITELIST_ENV_VAR,
    Scope,
    load_scope,
    parse_shell_whitelist_env,
)
from agentflow_computer_mcp.scope import ScopeDenied, check_shell


# ─────────── parse_shell_whitelist_env ───────────


def test_parse_returns_empty_for_none_and_blank() -> None:
    assert parse_shell_whitelist_env(None) == ()
    assert parse_shell_whitelist_env("") == ()
    assert parse_shell_whitelist_env("   \n  \n") == ()


def test_parse_comma_separated_single_line() -> None:
    result = parse_shell_whitelist_env("ls, cat, git, gh")
    assert result == ("ls", "cat", "git", "gh")


def test_parse_newline_separated() -> None:
    raw = "ls\ncat\ngit\ngh\n"
    assert parse_shell_whitelist_env(raw) == ("ls", "cat", "git", "gh")


def test_parse_mixed_comma_and_newline() -> None:
    raw = "ls, cat, head\ngit, gh\nnpm, pnpm"
    assert parse_shell_whitelist_env(raw) == (
        "ls",
        "cat",
        "head",
        "git",
        "gh",
        "npm",
        "pnpm",
    )


def test_parse_whitespace_separated() -> None:
    assert parse_shell_whitelist_env("ls cat git gh") == ("ls", "cat", "git", "gh")


def test_parse_ignores_blank_lines_and_comments() -> None:
    raw = """
    # baseline file utilities
    ls, cat, head

    # version-control
    git, gh
    # python
    python, pip
    """
    assert parse_shell_whitelist_env(raw) == (
        "ls",
        "cat",
        "head",
        "git",
        "gh",
        "python",
        "pip",
    )


def test_parse_deduplicates_preserving_order() -> None:
    # ordering matters: if AF_SHELL_WHITELIST repeats `git`, the first
    # occurrence wins so the file behaves the same on every pod boot.
    raw = "ls, git, cat, git, ls, pytest"
    assert parse_shell_whitelist_env(raw) == ("ls", "git", "cat", "pytest")


# ─────────── load_scope env integration ───────────


def test_load_scope_picks_up_env_when_no_file(tmp_path: Path) -> None:
    missing = tmp_path / "computer-scope.toml"
    with mock.patch.dict(os.environ, {SHELL_WHITELIST_ENV_VAR: "ls, git, pytest"}):
        scope = load_scope(missing)
    assert scope.shell_whitelist == ("ls", "git", "pytest")


def test_load_scope_empty_env_falls_back_to_defaults(tmp_path: Path) -> None:
    missing = tmp_path / "computer-scope.toml"
    env_without = {k: v for k, v in os.environ.items() if k != SHELL_WHITELIST_ENV_VAR}
    with mock.patch.dict(os.environ, env_without, clear=True):
        scope = load_scope(missing)
    assert scope.shell_whitelist == ()


def test_load_scope_merges_env_and_file(tmp_path: Path) -> None:
    # Env carries the baseline; file extends it with extras without
    # duplicating shared entries.
    scope_file = tmp_path / "computer-scope.toml"
    scope_file.write_text('shell_whitelist = ["git", "kubectl", "helm"]\n')

    with mock.patch.dict(os.environ, {SHELL_WHITELIST_ENV_VAR: "ls, git, pytest"}):
        scope = load_scope(scope_file)

    # env order first, dedup, then file-only extras appended
    assert scope.shell_whitelist == ("ls", "git", "pytest", "kubectl", "helm")


def test_load_scope_file_only_when_env_unset(tmp_path: Path) -> None:
    scope_file = tmp_path / "computer-scope.toml"
    scope_file.write_text('shell_whitelist = ["ls", "cat"]\n')
    env_without = {k: v for k, v in os.environ.items() if k != SHELL_WHITELIST_ENV_VAR}
    with mock.patch.dict(os.environ, env_without, clear=True):
        scope = load_scope(scope_file)
    assert scope.shell_whitelist == ("ls", "cat")


# ─────────── recursive-rm hard reject ───────────


def test_check_shell_rejects_rm_dash_r() -> None:
    scope = Scope(shell_whitelist=("rm", "ls"))
    with pytest.raises(ScopeDenied, match="recursive"):
        check_shell("rm -r /tmp/foo", scope)


def test_check_shell_rejects_rm_dash_capital_r() -> None:
    scope = Scope(shell_whitelist=("rm",))
    with pytest.raises(ScopeDenied, match="recursive"):
        check_shell("rm -R /tmp/foo", scope)


def test_check_shell_rejects_rm_dash_rf() -> None:
    scope = Scope(shell_whitelist=("rm",))
    for flag in ("-rf", "-fr", "-rfv", "-vrf", "-rfi"):
        with pytest.raises(ScopeDenied, match="recursive"):
            check_shell(f"rm {flag} /tmp/foo", scope)


def test_check_shell_rejects_rm_long_recursive() -> None:
    scope = Scope(shell_whitelist=("rm",))
    with pytest.raises(ScopeDenied, match="recursive"):
        check_shell("rm --recursive /tmp/foo", scope)


def test_check_shell_allows_plain_rm() -> None:
    scope = Scope(shell_whitelist=("rm",))
    assert check_shell("rm /tmp/oneoff.txt", scope) == "rm /tmp/oneoff.txt"
    assert check_shell("rm -f /tmp/oneoff.txt", scope) == "rm -f /tmp/oneoff.txt"
    assert check_shell("rm -v /tmp/x", scope) == "rm -v /tmp/x"
