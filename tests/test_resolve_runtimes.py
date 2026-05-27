"""Unit tests for ``agentflow_computer_mcp.driver.resolve_runtimes``.

Covered:

* manifest detection — engines.node, vite/next hints, requirements,
  pyproject, Cargo, go.mod
* version-constraint parsing (>=22, ^20, 18.x)
* install_node skip when current major >= required
* install_node install path triggers n download + n install + symlink
* install_python_deps creates venv + runs pip install
* empty workspace returns ``ok: True`` with no actions
* resolve_runtimes top-level orchestrator dispatches correctly
* CLI exit codes — workspace missing → 1, normal run → 0
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import agentflow_computer_mcp.driver.resolve_runtimes as rr


# --- shared fakes ---------------------------------------------------------


class FakeRunner:
    """Records every subprocess call and returns scripted results."""

    def __init__(self, scripted: dict[str, dict[str, Any]] | None = None):
        self.scripted = scripted or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd=None, *, timeout=120, env=None, check=False):
        self.calls.append(list(cmd))
        head = cmd[0]
        # Match the most specific suffix first ("install" before "node").
        for key, result in self.scripted.items():
            if key in cmd or key == head:
                return result
        return {"exit_code": 0, "stdout": "", "stderr": ""}


class FakeDownloader:
    def __init__(self, *, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, url, dest, timeout_s=60):
        self.calls.append((url, dest, timeout_s))
        if self.ok:
            # Touch dest so callers see the file.
            try:
                Path(dest).write_text("# fake n binary\n", encoding="utf-8")
            except OSError:
                pass
        return self.ok


# --- version-constraint parsing ------------------------------------------


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        (">=22", 22),
        (">=22.0.0", 22),
        ("^20", 20),
        ("~18.18.0", 18),
        ("18.x", 18),
        (">=20.10.0 <22", 20),
        ("v24", 24),
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_node_constraint(constraint, expected):
    assert rr._parse_node_constraint(constraint) == expected


# --- required_node_major from package.json --------------------------------


def _write_pkg(tmp_path: Path, payload: dict) -> str:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "package.json").write_text(json.dumps(payload), encoding="utf-8")
    return str(project)


def test_required_node_major_from_engines(tmp_path):
    proj = _write_pkg(tmp_path, {"engines": {"node": ">=24"}})
    assert rr.required_node_major(proj) == 24


def test_required_node_major_from_vite_hint(tmp_path):
    proj = _write_pkg(tmp_path, {"devDependencies": {"vite": "^7.0.0"}})
    # vite 7 → Node 20+ floor.
    assert rr.required_node_major(proj) == 20


def test_required_node_major_from_vitest_hint(tmp_path):
    proj = _write_pkg(tmp_path, {"devDependencies": {"vitest": "^3.1.0"}})
    # vitest 3 → Node 22+ floor.
    assert rr.required_node_major(proj) == 22


def test_required_node_major_combined_engines_and_hint(tmp_path):
    # engines says 18, but vitest 3 hint pushes the floor up to 22.
    proj = _write_pkg(
        tmp_path,
        {"engines": {"node": ">=18"}, "devDependencies": {"vitest": "^3"}},
    )
    assert rr.required_node_major(proj) == 22


def test_required_node_major_no_package_json(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    assert rr.required_node_major(str(proj)) is None


def test_required_node_major_old_vite_no_hint(tmp_path):
    # vite 4 is below the trigger (7) — no hint floor.
    proj = _write_pkg(tmp_path, {"devDependencies": {"vite": "^4.0.0"}})
    assert rr.required_node_major(proj) is None


# --- manifest presence helpers --------------------------------------------


def test_has_python_project_requirements(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "requirements.txt").write_text("flask==3.0\n")
    assert rr.has_python_project(str(proj)) is True


def test_has_python_project_pyproject(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert rr.has_python_project(str(proj)) is True


def test_has_python_project_none(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    assert rr.has_python_project(str(proj)) is False


def test_has_rust_and_go(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "Cargo.toml").write_text("[package]\nname='x'\n")
    (proj / "go.mod").write_text("module x\n")
    assert rr.has_rust_project(str(proj)) is True
    assert rr.has_go_project(str(proj)) is True


# --- install_node skip / install paths ------------------------------------


def test_install_node_skip_when_current_satisfies():
    fake_run = FakeRunner()
    fake_dl = FakeDownloader()
    result = rr.install_node(
        20,
        run=fake_run,
        http_download=fake_dl,
        current_major=lambda r: 22,
    )
    assert result["action"] == "skip"
    assert result["current_major"] == 22
    assert fake_dl.calls == []
    # node --version was never called — we shorted-circuit through the injected
    # current_major callable.
    assert all("node" not in c for c in fake_run.calls)


def test_install_node_installs_via_n(monkeypatch, tmp_path):
    """Old Node major → resolver downloads n + invokes n install <major>."""
    # Sandbox the install paths so the test never touches /usr/local.
    fake_n = tmp_path / "n"
    fake_runtimes = tmp_path / "runtimes"
    fake_node_bin = fake_runtimes / "bin" / "node"
    monkeypatch.setattr(rr, "N_BINARY_PATH", str(fake_n))
    monkeypatch.setattr(rr, "RUNTIMES_CACHE_DIR", str(fake_runtimes))

    # `n install` is mocked; emulate it by creating the node binary.
    def fake_run_impl(cmd, cwd=None, *, timeout=120, env=None, check=False):
        if cmd[0] == str(fake_n) and "install" in cmd:
            fake_node_bin.parent.mkdir(parents=True, exist_ok=True)
            fake_node_bin.write_text("#!/bin/sh\necho v24.0.0\n")
            fake_node_bin.chmod(0o755)
            return {"exit_code": 0, "stdout": "installed", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    fake_dl = FakeDownloader()
    # The function tries to symlink onto /usr/local/bin/node — patch Path.symlink_to
    # and Path.unlink to no-op when target sits under /usr/local.
    real_symlink = Path.symlink_to
    real_unlink = Path.unlink

    def safe_symlink(self, target):
        if str(self).startswith("/usr/local/"):
            return None
        return real_symlink(self, target)

    def safe_unlink(self, *args, **kwargs):
        if str(self).startswith("/usr/local/"):
            return None
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "symlink_to", safe_symlink)
    monkeypatch.setattr(Path, "unlink", safe_unlink)
    # /usr/local/bin/node existence check uses Path.exists — keep it real
    # but ensure our test path doesn't already have one.

    result = rr.install_node(
        24,
        run=fake_run_impl,
        http_download=fake_dl,
        current_major=lambda r: 18,
    )
    assert result["action"] == "install"
    assert result["required_major"] == 24
    # Download was triggered (n binary missing).
    assert len(fake_dl.calls) == 1
    assert fake_dl.calls[0][0] == rr.N_DOWNLOAD_URL


# --- install_python_deps --------------------------------------------------


def test_install_python_deps_creates_venv(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "requirements.txt").write_text("flask\n")

    venv_python = proj / ".venv" / "bin" / "python"
    fake_pip_calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, *, timeout=120, env=None, check=False):
        # python -m venv → create the venv layout the function probes for.
        if "venv" in cmd:
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text("#!/bin/sh\n")
            (venv_python.parent / "pip").write_text("#!/bin/sh\n")
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if "pip" in cmd[0] or cmd[0].endswith("/pip"):
            fake_pip_calls.append(list(cmd))
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    result = rr.install_python_deps(str(proj), run=fake_run)
    assert result["action"] == "install"
    assert venv_python.is_file()
    # Exactly one pip install -r requirements.txt invocation.
    pip_installs = [c for c in fake_pip_calls if c[1:3] == ["install", "-r"]]
    assert len(pip_installs) == 1


def test_install_python_deps_skip_when_no_manifest(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    result = rr.install_python_deps(str(proj), run=FakeRunner())
    assert result["action"] == "skip"


def test_install_python_deps_idempotent(tmp_path):
    """Pre-existing .venv is reused; venv creation is skipped."""
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "requirements.txt").write_text("flask\n")
    venv_python = proj / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    (venv_python.parent / "pip").write_text("#!/bin/sh\n")

    fake = FakeRunner()
    result = rr.install_python_deps(str(proj), run=fake)
    assert result["action"] == "install"
    # No `python -m venv` invocation — venv was reused.
    assert not any("venv" in c for c in fake.calls)


# --- top-level orchestrator -----------------------------------------------


def test_resolve_runtimes_empty_workspace(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    fake_run = FakeRunner()
    fake_dl = FakeDownloader()
    result = rr.resolve_runtimes(
        str(proj),
        run=fake_run,
        http_download=fake_dl,
        current_node=lambda r: 22,
    )
    assert result["ok"] is True
    assert result["actions"] == []


def test_resolve_runtimes_missing_workspace(tmp_path):
    result = rr.resolve_runtimes(str(tmp_path / "does-not-exist"))
    assert result["ok"] is False
    assert result["error"] == "workspace_missing"


def test_resolve_runtimes_node_only_skip(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "package.json").write_text(json.dumps({"engines": {"node": ">=20"}}))

    fake_run = FakeRunner()
    fake_dl = FakeDownloader()
    result = rr.resolve_runtimes(
        str(proj),
        run=fake_run,
        http_download=fake_dl,
        current_node=lambda r: 22,
    )
    assert result["ok"] is True
    runtimes = [a["runtime"] for a in result["actions"]]
    assert runtimes == ["node"]
    assert result["actions"][0]["action"] == "skip"


def test_resolve_runtimes_python_path(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "requirements.txt").write_text("flask\n")

    def fake_run(cmd, cwd=None, *, timeout=120, env=None, check=False):
        if "venv" in cmd:
            venv_python = proj / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text("#!/bin/sh\n")
            (venv_python.parent / "pip").write_text("#!/bin/sh\n")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    result = rr.resolve_runtimes(
        str(proj), run=fake_run, http_download=FakeDownloader(), current_node=lambda r: 22
    )
    assert result["ok"] is True
    runtimes = [a["runtime"] for a in result["actions"]]
    assert runtimes == ["python"]


# --- CLI ------------------------------------------------------------------


def test_cli_missing_workspace_exits_1(tmp_path, capsys):
    code = rr._cli([str(tmp_path / "nope")])
    assert code == 1


def test_cli_no_arg_returns_2(capsys):
    code = rr._cli([])
    assert code == 2


def test_cli_empty_workspace_exits_0(tmp_path, monkeypatch):
    # current_node lookup hits real `node --version` via _default_run; stub it
    # so the test doesn't depend on the host toolchain.
    monkeypatch.setattr(rr, "_current_node_major", lambda r: 22)
    proj = tmp_path / "p"
    proj.mkdir()
    code = rr._cli([str(proj)])
    assert code == 0
