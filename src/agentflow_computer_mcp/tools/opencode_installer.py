"""Install OpenCode (https://github.com/sst/opencode) into ~/.agentflow/bin and
patch its config so it routes through the AgentFlow LLM gateway.

OpenCode ships as a single Go binary plus a TUI; releases are at
``github.com/sst/opencode/releases`` with per-OS archives:

- ``opencode-darwin-arm64.zip`` / ``opencode-darwin-x64.zip``
- ``opencode-linux-arm64.tar.gz`` / ``opencode-linux-x64.tar.gz``
- ``opencode-windows-arm64.zip`` / ``opencode-windows-x64.zip``

After install we write/merge ``opencode.json`` so the provider chain points at
``https://agentflow.website/_agents/llm/v1`` (OpenAI-compatible facade backed by
``llm-cabinet`` billing). Existing user providers are preserved — we only add
or update the ``agentflow`` entry and switch the default ``model`` field.

This module deliberately does NOT use the daemon's standard ``Scope`` /
``check_path`` gate for the config file: ``~/.config`` lives inside
``HARD_DENY_PATHS`` (secret hygiene for the rest of the daemon). For
OpenCode's config we own a narrow whitelist instead — writes are restricted
to ``opencode_config_path()`` only, which is auditable from the cabinet.
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

# Single source of truth for the AgentFlow LLM gateway. The gateway exposes
# both OpenAI-compatible (``/llm/v1/chat/completions``) and Anthropic-format
# (``/llm/v1/messages``) endpoints; @ai-sdk/* providers append the trailing
# path themselves, so we pass them the ``/llm/v1`` prefix and they fill in
# ``/chat/completions`` or ``/messages`` based on which SDK is used.
DEFAULT_AF_BASE_URL = "https://agentflow.website/_agents/llm/v1"

# Default model OpenCode will select after a fresh patch. Lives on the
# AgentFlow LLM facade as ``claude-opus-4-7`` (Anthropic format), routed by
# llm-router through the east-api-3 proxy. We pin a Claude model rather
# than a GPT one because the use case is "Claude Code-style coding via
# our gateway" — same model family the user already pays for elsewhere.
DEFAULT_AF_MODEL = "claude-opus-4-7"

# Github releases endpoint — single canonical upstream. We resolve ``latest``
# through the API rather than the redirect to read the tag name without
# following the download.
GITHUB_API_LATEST = "https://api.github.com/repos/sst/opencode/releases/latest"
GITHUB_API_TAG = "https://api.github.com/repos/sst/opencode/releases/tags/{tag}"

# When picking an asset for the host we accept the first match by suffix in
# this order. ``baseline`` builds are skipped — they target older CPUs and
# trade speed for compatibility; modern hardware should fetch the regular
# build.
ASSET_PREFERENCE: dict[tuple[str, str], tuple[str, ...]] = {
    ("darwin", "arm64"): ("opencode-darwin-arm64.zip",),
    ("darwin", "x64"): ("opencode-darwin-x64.zip", "opencode-darwin-x64-baseline.zip"),
    ("linux", "arm64"): ("opencode-linux-arm64.tar.gz",),
    ("linux", "x64"): ("opencode-linux-x64.tar.gz", "opencode-linux-x64-baseline.tar.gz"),
    ("windows", "arm64"): ("opencode-windows-arm64.zip",),
    ("windows", "x64"): ("opencode-windows-x64.zip", "opencode-windows-x64-baseline.zip"),
}


def detect_platform() -> tuple[str, str]:
    """Return ``(os_slug, arch_slug)`` for the running host.

    ``os_slug`` is one of ``darwin`` / ``linux`` / ``windows``.
    ``arch_slug`` is ``arm64`` or ``x64`` — matches OpenCode's asset naming.
    """
    sys_name = platform.system().lower()
    if sys_name.startswith("darwin"):
        os_slug = "darwin"
    elif sys_name.startswith("linux"):
        os_slug = "linux"
    elif sys_name.startswith("windows"):
        os_slug = "windows"
    else:
        raise RuntimeError(f"unsupported_os: {platform.system()}")

    mach = (platform.machine() or "").lower()
    if mach in ("arm64", "aarch64"):
        arch_slug = "arm64"
    elif mach in ("x86_64", "amd64", "x64"):
        arch_slug = "x64"
    else:
        raise RuntimeError(f"unsupported_arch: {platform.machine()}")

    return os_slug, arch_slug


def opencode_install_dir() -> Path:
    """Per-OS install directory for the binary. Lives next to other daemon
    binaries under ``~/.agentflow/bin`` so the cabinet can clean it up the
    same way it cleans up the daemon itself."""
    return Path.home() / ".agentflow" / "bin"


def opencode_config_path() -> Path:
    """Per-OS path for ``opencode.json``.

    - macOS / Linux: ``~/.config/opencode/opencode.json``
    - Windows: ``%APPDATA%\\opencode\\opencode.json`` (falls back to
      ``~/AppData/Roaming/opencode/opencode.json`` if ``APPDATA`` is empty).
    """
    os_slug, _ = detect_platform()
    if os_slug == "windows":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "opencode" / "opencode.json"
    return Path.home() / ".config" / "opencode" / "opencode.json"


def opencode_binary_path() -> Path:
    """Final binary location after install. ``.exe`` on Windows."""
    os_slug, _ = detect_platform()
    name = "opencode.exe" if os_slug == "windows" else "opencode"
    return opencode_install_dir() / name


def _resolve_release(version: str) -> dict[str, Any]:
    """Hit the GitHub API and return ``{tag_name, assets:[{name, url}]}``.

    ``version`` may be ``"latest"`` or an explicit tag like ``"v1.15.10"``.
    """
    if version == "latest":
        url = GITHUB_API_LATEST
    else:
        tag = version if version.startswith("v") else f"v{version}"
        url = GITHUB_API_TAG.format(tag=tag)

    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "agentflow-computer-mcp"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
    payload = json.loads(body)
    return {
        "tag_name": payload.get("tag_name"),
        "assets": [
            {"name": a.get("name"), "url": a.get("browser_download_url")}
            for a in payload.get("assets", [])
            if a.get("name") and a.get("browser_download_url")
        ],
    }


def _pick_asset(assets: list[dict[str, str]], os_slug: str, arch_slug: str) -> dict[str, str]:
    """Pick the first asset matching the host preference list.

    Raises ``RuntimeError`` if nothing matches — typically means the release
    dropped a target or the host architecture is exotic.
    """
    key = (os_slug, arch_slug)
    candidates = ASSET_PREFERENCE.get(key)
    if not candidates:
        raise RuntimeError(f"no_asset_pattern_for: {os_slug}/{arch_slug}")
    by_name = {a["name"]: a for a in assets}
    for name in candidates:
        if name in by_name:
            return by_name[name]
    raise RuntimeError(
        f"no_matching_asset for {os_slug}/{arch_slug}; tried {candidates}; "
        f"release assets: {[a['name'] for a in assets[:8]]}..."
    )


def _download(url: str, dest: Path) -> None:
    """Stream a URL to ``dest`` without buffering everything in memory."""
    req = urllib.request.Request(url, headers={"User-Agent": "agentflow-computer-mcp"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as fp:
        shutil.copyfileobj(resp, fp, length=64 * 1024)


def _extract(archive: Path, dest_dir: Path) -> Path:
    """Unpack ``archive`` (zip or tar.gz) into ``dest_dir`` and return the
    path of the ``opencode`` (or ``opencode.exe``) binary inside.

    OpenCode archives ship the binary at the archive root, but we walk the
    extracted tree just in case future releases nest it.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
    elif name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as tf:
            # ``filter='data'`` blocks dangerous tar features (links escaping
            # the dest, abs paths, weird perms) — Python 3.12+ requires it
            # explicitly or warns; ``opencode-*`` archives ship plain files.
            tf.extractall(dest_dir, filter="data")
    else:
        raise RuntimeError(f"unsupported_archive_format: {archive.name}")

    for candidate in dest_dir.rglob("*"):
        if candidate.is_file() and candidate.name in ("opencode", "opencode.exe"):
            return candidate
    raise RuntimeError(f"binary_not_found_in_archive: {archive.name}")


def install_opencode(version: str = "latest") -> dict[str, Any]:
    """Download + install OpenCode for the current host.

    Returns ``{ok, version, binary_path, bytes, asset}``. Re-runs are
    idempotent — the old binary is overwritten in place.
    """
    os_slug, arch_slug = detect_platform()
    release = _resolve_release(version)
    asset = _pick_asset(release["assets"], os_slug, arch_slug)

    install_dir = opencode_install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)
    final_binary = opencode_binary_path()

    with tempfile.TemporaryDirectory(prefix="opencode-dl-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / asset["name"]
        try:
            _download(asset["url"], archive)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"download_failed: {exc}") from exc

        extracted = _extract(archive, tmp_path / "unpacked")
        # Replace the prior binary atomically when possible. shutil.move
        # handles the cross-filesystem case (tmp dir on a different volume).
        if final_binary.exists():
            final_binary.unlink()
        shutil.move(str(extracted), str(final_binary))

    if os_slug != "windows":
        mode = final_binary.stat().st_mode
        final_binary.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return {
        "ok": True,
        "version": release["tag_name"],
        "binary_path": str(final_binary),
        "bytes": final_binary.stat().st_size,
        "asset": asset["name"],
        "os": os_slug,
        "arch": arch_slug,
    }


def _read_existing_config(path: Path) -> dict[str, Any]:
    """Load existing opencode.json or return ``{}``. Tolerates a missing
    file, an empty file, or a corrupt JSON file (logs and starts fresh)."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        # Preserve the corrupted file as a sibling so we never lose user data.
        backup = path.with_suffix(".json.broken")
        with contextlib.suppress(OSError):
            shutil.copy2(path, backup)
        return {}


# Alias-mode constants. The backend `/me/llm/aliases` POST registers a
# user-defined alias name (e.g. "flow") that resolves to any enabled
# upstream. OpenCode addresses providers as `<provider>/<model>`, so the
# alias has to ride on one of the two AgentFlow provider entries below.
# `openai` is the default mode because the codex CLI + OpenAI-compatible
# tooling family is the common path the alias feature ships for.
ALIAS_MODE_OPENAI = "openai"
ALIAS_MODE_ANTHROPIC = "anthropic"
_AF_PROVIDER_FOR_MODE = {
    ALIAS_MODE_OPENAI: "agentflow-openai",
    ALIAS_MODE_ANTHROPIC: "agentflow",
}
ALIAS_NAME_PATTERN = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


def _normalise_alias(raw: str | None) -> str | None:
    """Lowercase + trim an alias name and enforce the backend regex.

    Returns the cleaned string or ``None`` when ``raw`` is falsy. Raises
    ``ValueError`` when ``raw`` is set but does not match the pattern —
    the backend would reject it anyway and we want the failure to surface
    on the daemon side before we touch ``opencode.json``.
    """
    if raw is None:
        return None
    cleaned = str(raw).strip().lower()
    if not cleaned:
        return None
    if not ALIAS_NAME_PATTERN.match(cleaned):
        raise ValueError(
            f"invalid_alias: {raw!r} must match /^[a-z0-9][a-z0-9_-]{{1,63}}$/"
        )
    return cleaned


def patch_opencode_config(
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    alias: str | None = None,
    alias_mode: str = ALIAS_MODE_OPENAI,
) -> dict[str, Any]:
    """Write/merge ``opencode.json`` so OpenCode routes through AgentFlow.

    Two providers are registered:

    - ``agentflow`` — Anthropic-format upstream (via ``@ai-sdk/anthropic``)
      for the Claude family. The gateway accepts these on
      ``/llm/v1/messages``.
    - ``agentflow-openai`` — OpenAI-format upstream (via
      ``@ai-sdk/openai-compatible``) for GPT-family models. The gateway
      accepts these on ``/llm/v1/chat/completions``.

    Existing ``provider`` entries are preserved. The top-level ``model`` is
    rewritten to ``agentflow/<chosen_model>`` only when the existing value
    is empty or already points at one of the AgentFlow provider slugs — we
    never clobber a deliberate user choice for openrouter, anthropic
    upstream, etc.

    When ``alias`` is provided, the matching AgentFlow provider gets the
    alias appended to its ``models`` map and the top-level ``model`` is
    set to ``<provider>/<alias>``. The provider is chosen by
    ``alias_mode``: ``openai`` (default) attaches the alias to
    ``agentflow-openai`` (codex CLI, opencode tools that use the
    chat/completions wire format), ``anthropic`` to ``agentflow`` (Claude
    Code-style OpenCode clients that speak the Anthropic Messages API).

    The alias name is validated against the backend regex
    (``/^[a-z0-9][a-z0-9_-]{1,63}$/``) so a bad name fails locally before
    we touch the config file.
    """
    if not api_key:
        raise ValueError("api_key is required")

    target = opencode_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    config = _read_existing_config(target)
    provider = dict(config.get("provider") or {})

    chosen_base = base_url or DEFAULT_AF_BASE_URL
    chosen_model = model or DEFAULT_AF_MODEL
    chosen_alias = _normalise_alias(alias)
    if chosen_alias is not None and alias_mode not in _AF_PROVIDER_FOR_MODE:
        raise ValueError(
            f"invalid_alias_mode: {alias_mode!r}; expected one of "
            f"{sorted(_AF_PROVIDER_FOR_MODE)}"
        )

    provider["agentflow"] = {
        "npm": "@ai-sdk/anthropic",
        "name": "AgentFlow (Claude)",
        "options": {
            "baseURL": chosen_base,
            "apiKey": api_key,
        },
        "models": {
            "claude-opus-4-7": {"name": "Claude Opus 4.7 (AgentFlow)"},
            "claude-haiku-4-5": {"name": "Claude Haiku 4.5 (AgentFlow)"},
        },
    }
    provider["agentflow-openai"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "AgentFlow (GPT)",
        "options": {
            "baseURL": chosen_base,
            "apiKey": api_key,
        },
        "models": {
            "gpt-5.4": {"name": "GPT-5.4 (AgentFlow)"},
            "gpt-5.5": {"name": "GPT-5.5 (AgentFlow)"},
            "gpt-5.3-codex": {"name": "GPT-5.3 Codex (AgentFlow)"},
        },
    }

    chosen_provider_slug = "agentflow"
    if chosen_alias is not None:
        chosen_provider_slug = _AF_PROVIDER_FOR_MODE[alias_mode]
        # Append the alias to that provider's models map so OpenCode lists
        # it in /models and accepts it as a target. We don't drop the
        # canonical model ids — the user can still flip to them by hand.
        provider_block = provider[chosen_provider_slug]
        models_map = dict(provider_block.get("models") or {})
        models_map[chosen_alias] = {
            "name": f"AgentFlow alias «{chosen_alias}»",
        }
        provider_block["models"] = models_map
        provider[chosen_provider_slug] = provider_block

    config["provider"] = provider
    config.setdefault("$schema", "https://opencode.ai/config.json")

    af_slugs = ("agentflow/", "agentflow-openai/")
    existing_model = str(config.get("model") or "")
    if chosen_alias is not None:
        # Alias path: always pin the top-level model to the alias so the
        # owner can swap upstream from the cabinet without re-running
        # patch_opencode_config. Overrides existing user model only when
        # it was already an AgentFlow entry (same guard as the non-alias
        # branch).
        if not existing_model or existing_model.startswith(af_slugs):
            config["model"] = f"{chosen_provider_slug}/{chosen_alias}"
    else:
        if not existing_model or existing_model.startswith(af_slugs):
            config["model"] = f"agentflow/{chosen_model}"

    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Filesystems that don't support chmod (FAT, some network mounts) —
    # the file is still written; perms just stay at the FS default.
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    tmp.replace(target)

    return {
        "ok": True,
        "config_path": str(target),
        "provider": chosen_provider_slug,
        "model": config["model"],
        "base_url": chosen_base,
        "alias": chosen_alias,
        "alias_mode": alias_mode if chosen_alias is not None else None,
    }


def set_opencode_default_alias(
    alias: str,
    alias_mode: str = ALIAS_MODE_OPENAI,
) -> dict[str, Any]:
    """Repoint OpenCode's default ``model`` field at an AgentFlow alias.

    Targeted edit: opens the existing ``opencode.json``, validates the
    alias name + mode, swaps the ``model`` field to
    ``<provider>/<alias>``, and writes it back atomically. Does not touch
    ``provider`` entries — those have to be set up first via
    ``patch_opencode_config`` (which knows the API key). If the config is
    missing or has no AgentFlow provider entry yet the call returns
    ``ok=False`` with a hint instead of silently writing a half-config.

    Use this when the owner has already wired OpenCode through AgentFlow
    once (provider + api key in place) and now wants to flip the default
    model to a new alias name without re-supplying the key.
    """
    cleaned = _normalise_alias(alias)
    if cleaned is None:
        raise ValueError("alias is required")
    if alias_mode not in _AF_PROVIDER_FOR_MODE:
        raise ValueError(
            f"invalid_alias_mode: {alias_mode!r}; expected one of "
            f"{sorted(_AF_PROVIDER_FOR_MODE)}"
        )

    target = opencode_config_path()
    if not target.exists():
        return {
            "ok": False,
            "error": "config_missing",
            "config_path": str(target),
            "hint": "Run patch_opencode_config(api_key=...) first.",
        }

    config = _read_existing_config(target)
    provider_slug = _AF_PROVIDER_FOR_MODE[alias_mode]
    provider = dict(config.get("provider") or {})
    if provider_slug not in provider:
        return {
            "ok": False,
            "error": "provider_missing",
            "provider": provider_slug,
            "config_path": str(target),
            "hint": (
                "Run patch_opencode_config(api_key=...) first so the "
                f"{provider_slug} provider block exists."
            ),
        }

    # Register the alias on the provider's models map even if the backend
    # is the source of truth — OpenCode needs at least an empty entry to
    # accept the model id in `/models` and as a CLI target.
    provider_block = dict(provider[provider_slug])
    models_map = dict(provider_block.get("models") or {})
    models_map.setdefault(cleaned, {"name": f"AgentFlow alias «{cleaned}»"})
    provider_block["models"] = models_map
    provider[provider_slug] = provider_block
    config["provider"] = provider

    new_model = f"{provider_slug}/{cleaned}"
    previous_model = str(config.get("model") or "")
    config["model"] = new_model

    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    tmp.replace(target)

    return {
        "ok": True,
        "config_path": str(target),
        "alias": cleaned,
        "alias_mode": alias_mode,
        "provider": provider_slug,
        "model": new_model,
        "previous_model": previous_model or None,
    }


__all__ = [
    "ALIAS_MODE_ANTHROPIC",
    "ALIAS_MODE_OPENAI",
    "DEFAULT_AF_BASE_URL",
    "DEFAULT_AF_MODEL",
    "detect_platform",
    "install_opencode",
    "opencode_binary_path",
    "opencode_config_path",
    "opencode_install_dir",
    "patch_opencode_config",
    "set_opencode_default_alias",
]
