"""Tests for the OpenCode installer + config patcher.

Network is mocked at the ``urllib.request.urlopen`` boundary so the test
suite stays offline. Filesystem writes go through ``tmp_path`` via
``monkeypatch`` of ``Path.home()`` so we never touch the developer's real
``~/.agentflow`` or ``~/.config/opencode``.
"""
from __future__ import annotations

import io
import json
import platform
import tarfile
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agentflow_computer_mcp.tools import opencode_installer

# ─── detect_platform ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", ("darwin", "arm64")),
        ("Darwin", "x86_64", ("darwin", "x64")),
        ("Linux", "aarch64", ("linux", "arm64")),
        ("Linux", "x86_64", ("linux", "x64")),
        ("Windows", "AMD64", ("windows", "x64")),
        ("Windows", "ARM64", ("windows", "arm64")),
    ],
)
def test_detect_platform_maps_all_supported_hosts(
    system: str, machine: str, expected: tuple[str, str]
) -> None:
    with patch.object(platform, "system", return_value=system), patch.object(
        platform, "machine", return_value=machine
    ):
        assert opencode_installer.detect_platform() == expected


def test_detect_platform_rejects_unsupported_os() -> None:
    with patch.object(platform, "system", return_value="FreeBSD"), patch.object(
        platform, "machine", return_value="amd64"
    ), pytest.raises(RuntimeError, match="unsupported_os"):
        opencode_installer.detect_platform()


def test_detect_platform_rejects_unsupported_arch() -> None:
    with patch.object(platform, "system", return_value="Linux"), patch.object(
        platform, "machine", return_value="mips64"
    ), pytest.raises(RuntimeError, match="unsupported_arch"):
        opencode_installer.detect_platform()


# ─── path helpers ────────────────────────────────────────────────────────────


def test_install_dir_lives_under_agentflow_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert opencode_installer.opencode_install_dir() == tmp_path / ".agentflow" / "bin"


def test_config_path_unix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ):
        expected = tmp_path / ".config" / "opencode" / "opencode.json"
        assert opencode_installer.opencode_config_path() == expected


def test_config_path_windows_honors_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("APPDATA", str(appdata))
    with patch.object(platform, "system", return_value="Windows"), patch.object(
        platform, "machine", return_value="AMD64"
    ):
        expected = appdata / "opencode" / "opencode.json"
        assert opencode_installer.opencode_config_path() == expected


def test_binary_path_has_exe_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Windows"), patch.object(
        platform, "machine", return_value="AMD64"
    ):
        assert opencode_installer.opencode_binary_path().name == "opencode.exe"


def test_binary_path_no_suffix_on_unix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Linux"), patch.object(
        platform, "machine", return_value="x86_64"
    ):
        assert opencode_installer.opencode_binary_path().name == "opencode"


# ─── _pick_asset ─────────────────────────────────────────────────────────────


def test_pick_asset_chooses_preferred_first() -> None:
    assets = [
        {"name": "opencode-darwin-arm64.zip", "url": "https://example/u1"},
        {"name": "opencode-linux-x64.tar.gz", "url": "https://example/u2"},
    ]
    chosen = opencode_installer._pick_asset(assets, "darwin", "arm64")
    assert chosen["name"] == "opencode-darwin-arm64.zip"


def test_pick_asset_falls_through_to_baseline() -> None:
    # Regular x64 asset missing — fallback to baseline build.
    assets = [{"name": "opencode-darwin-x64-baseline.zip", "url": "https://example/u"}]
    chosen = opencode_installer._pick_asset(assets, "darwin", "x64")
    assert chosen["name"] == "opencode-darwin-x64-baseline.zip"


def test_pick_asset_raises_when_no_match() -> None:
    assets = [{"name": "opencode-linux-x64.tar.gz", "url": "https://example/u"}]
    with pytest.raises(RuntimeError, match="no_matching_asset"):
        opencode_installer._pick_asset(assets, "darwin", "arm64")


# ─── install_opencode (mocked download) ──────────────────────────────────────


def _make_fake_zip(binary_name: str = "opencode", payload: bytes = b"#!/bin/sh\necho stub\n") -> bytes:
    """Create an in-memory zip with a single binary file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(binary_name, payload)
    return buf.getvalue()


def _make_fake_tgz(binary_name: str = "opencode", payload: bytes = b"#!/bin/sh\necho stub\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=binary_name)
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if n == -1 or n is None:
            data = self._body[self._pos:]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos : self._pos + n]
        self._pos += len(data)
        return data

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_: Any) -> None:
        pass


def _fake_urlopen_factory(release_json: dict[str, Any], asset_bytes: bytes):
    """Return a urlopen-replacement that serves the release JSON then the asset."""

    def _fake_urlopen(req, timeout: int = 10):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.github.com" in url:
            return _FakeResp(json.dumps(release_json).encode("utf-8"))
        return _FakeResp(asset_bytes)

    return _fake_urlopen


def test_install_opencode_happy_path_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ):
        release = {
            "tag_name": "v1.15.10",
            "assets": [
                {
                    "name": "opencode-darwin-arm64.zip",
                    "browser_download_url": "https://example/opencode-darwin-arm64.zip",
                }
            ],
        }
        fake = _fake_urlopen_factory(release, _make_fake_zip("opencode"))
        with patch("urllib.request.urlopen", fake):
            result = opencode_installer.install_opencode()

    assert result["ok"] is True
    assert result["version"] == "v1.15.10"
    assert result["asset"] == "opencode-darwin-arm64.zip"
    binary = Path(result["binary_path"])
    assert binary.exists()
    # Unix executable bit set.
    assert binary.stat().st_mode & 0o111


def test_install_opencode_happy_path_linux_tgz(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Linux"), patch.object(
        platform, "machine", return_value="x86_64"
    ):
        release = {
            "tag_name": "v1.15.10",
            "assets": [
                {
                    "name": "opencode-linux-x64.tar.gz",
                    "browser_download_url": "https://example/opencode-linux-x64.tar.gz",
                }
            ],
        }
        fake = _fake_urlopen_factory(release, _make_fake_tgz("opencode"))
        with patch("urllib.request.urlopen", fake):
            result = opencode_installer.install_opencode()

    assert result["asset"] == "opencode-linux-x64.tar.gz"
    assert Path(result["binary_path"]).exists()


def test_install_opencode_happy_path_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Windows"), patch.object(
        platform, "machine", return_value="AMD64"
    ):
        release = {
            "tag_name": "v1.15.10",
            "assets": [
                {
                    "name": "opencode-windows-x64.zip",
                    "browser_download_url": "https://example/opencode-windows-x64.zip",
                }
            ],
        }
        fake = _fake_urlopen_factory(release, _make_fake_zip("opencode.exe"))
        with patch("urllib.request.urlopen", fake):
            result = opencode_installer.install_opencode()

    assert result["asset"] == "opencode-windows-x64.zip"
    binary = Path(result["binary_path"])
    assert binary.name == "opencode.exe"
    assert binary.exists()


def test_install_opencode_rejects_missing_asset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ):
        release = {
            "tag_name": "v1.15.10",
            "assets": [
                {
                    "name": "opencode-linux-x64.tar.gz",
                    "browser_download_url": "https://example/x",
                }
            ],
        }
        fake = _fake_urlopen_factory(release, b"")
        with patch("urllib.request.urlopen", fake), pytest.raises(RuntimeError, match="no_matching_asset"):
            opencode_installer.install_opencode()


# ─── patch_opencode_config ───────────────────────────────────────────────────


def test_patch_config_creates_fresh_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ):
        result = opencode_installer.patch_opencode_config(api_key="af_live_fake")

    config_path = Path(result["config_path"])
    assert config_path.exists()
    data = json.loads(config_path.read_text())
    assert data["provider"]["agentflow"]["npm"] == "@ai-sdk/anthropic"
    assert data["provider"]["agentflow"]["options"]["apiKey"] == "af_live_fake"
    assert data["provider"]["agentflow"]["options"]["baseURL"] == opencode_installer.DEFAULT_AF_BASE_URL
    assert data["provider"]["agentflow-openai"]["npm"] == "@ai-sdk/openai-compatible"
    assert data["provider"]["agentflow-openai"]["options"]["apiKey"] == "af_live_fake"
    assert data["model"].startswith("agentflow/")
    assert data["$schema"] == "https://opencode.ai/config.json"


def test_patch_config_preserves_existing_providers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ):
        cfg = opencode_installer.opencode_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            json.dumps(
                {
                    "provider": {
                        "openrouter": {
                            "npm": "@openrouter/ai-sdk-provider",
                            "options": {"apiKey": "sk-or-v1-keep-me"},
                        }
                    },
                    "model": "openrouter/anthropic/claude-3.5-sonnet",
                }
            )
        )
        opencode_installer.patch_opencode_config(api_key="af_live_new")

        data = json.loads(cfg.read_text())

    # User's openrouter provider survived.
    assert "openrouter" in data["provider"]
    assert data["provider"]["openrouter"]["options"]["apiKey"] == "sk-or-v1-keep-me"
    # AgentFlow provider added.
    assert data["provider"]["agentflow"]["options"]["apiKey"] == "af_live_new"
    # User's deliberate model choice preserved (not pointing at agentflow/*).
    assert data["model"] == "openrouter/anthropic/claude-3.5-sonnet"


def test_patch_config_updates_when_pointing_at_agentflow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ):
        cfg = opencode_installer.opencode_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"model": "agentflow/old-model"}))
        opencode_installer.patch_opencode_config(api_key="af_live_x", model="claude-haiku-4-5")

        data = json.loads(cfg.read_text())

    assert data["model"] == "agentflow/claude-haiku-4-5"


def test_patch_config_updates_when_pointing_at_agentflow_openai(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``agentflow-openai/*`` is also treated as AgentFlow-owned and may be rewritten."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ):
        cfg = opencode_installer.opencode_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"model": "agentflow-openai/gpt-5.4"}))
        opencode_installer.patch_opencode_config(api_key="af_live_x", model="claude-opus-4-7")

        data = json.loads(cfg.read_text())

    assert data["model"] == "agentflow/claude-opus-4-7"


def test_patch_config_recovers_from_corrupt_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ):
        cfg = opencode_installer.opencode_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{this is not [valid json")
        opencode_installer.patch_opencode_config(api_key="af_live_x")

        data = json.loads(cfg.read_text())

    # Backup of the corrupt file was kept beside the recovered config.
    assert (cfg.with_suffix(".json.broken")).exists()
    assert data["provider"]["agentflow"]["options"]["apiKey"] == "af_live_x"


def test_patch_config_rejects_empty_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ), pytest.raises(ValueError, match="api_key is required"):
        opencode_installer.patch_opencode_config(api_key="")


def test_patch_config_accepts_custom_base_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch.object(platform, "system", return_value="Darwin"), patch.object(
        platform, "machine", return_value="arm64"
    ):
        result = opencode_installer.patch_opencode_config(
            api_key="af_live_x", base_url="https://staging.agentflow.website/_agents/llm/v1"
        )
        data = json.loads(Path(result["config_path"]).read_text())
    assert data["provider"]["agentflow"]["options"]["baseURL"] == (
        "https://staging.agentflow.website/_agents/llm/v1"
    )


# ─── descriptor registration ─────────────────────────────────────────────────


def test_llm_descriptors_include_opencode_tools() -> None:
    from agentflow_computer_mcp.driver.desktop_tools import DESKTOP_TOOLS

    names = {t["name"] for t in DESKTOP_TOOLS}
    assert "opencode_install" in names
    assert "opencode_patch_config" in names
    assert "opencode_status" in names


def test_mcp_tool_names_include_opencode() -> None:
    from agentflow_computer_mcp.server import TOOL_NAMES

    assert "computer.opencode.install" in TOOL_NAMES
    assert "computer.opencode.patch_config" in TOOL_NAMES
    assert "computer.opencode.status" in TOOL_NAMES
