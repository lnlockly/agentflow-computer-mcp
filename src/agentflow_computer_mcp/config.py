from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HARD_DENY_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.config",
    "~/Library/Keychains",
    "~/.aws",
    "~/.gnupg",
)

DEFAULT_CONFIRM_BEFORE: tuple[str, ...] = ("computer.fs.write", "computer.shell.exec")

# Hosted daemons (kind=daemon pods, AF_HOSTED=1 env) have no user sitting at
# a screen to dismiss native confirm dialogs — every confirm() would block
# forever and then auto-deny. The owner already configures the device's
# scope via /me/devices/:id/scope (sent over WS at every task dispatch),
# so the confirm gate is redundant in that context. Default to "no
# pre-confirms" for hosted; the cabinet remains the single source of truth.
HOSTED_MODE: bool = os.environ.get("AF_HOSTED") == "1"
if HOSTED_MODE:
    DEFAULT_CONFIRM_BEFORE = ()

# Env-driven shell whitelist for hosted daemons. The autonomous LLM
# routinely emits plans that need `git fetch`, `pytest`, `pip install`,
# `npm ci` etc. — without a whitelist, every `code_run_command` returns
# `shell_whitelist is empty; shell.exec disabled` and the session ends
# in COMPLETION_BLOCKED. Owner-controlled override per-device still
# works via /me/devices/:id/scope (and that gets merged on top via the
# WS task_dispatch scope); this env var only sets the daemon's baseline.
SHELL_WHITELIST_ENV_VAR = "AF_SHELL_WHITELIST"


def parse_shell_whitelist_env(value: str | None) -> tuple[str, ...]:
    """Parse `AF_SHELL_WHITELIST` into an ordered, deduplicated tuple.

    Tolerates three shapes the entrypoint may use, depending on whether
    the value is set inline (Helm), via printf, or via a multi-line
    heredoc:
      - comma-separated: ``"ls, cat, git, gh"``
      - newline-separated: one program per line
      - whitespace-separated: ``"ls cat git gh"``
    Blank lines and `# comments` are ignored. Repeats collapse to the
    first occurrence to keep order stable across env churn.
    """
    if not value:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    # Normalize comma/newline → newline so we can split on a single token,
    # then strip surrounding whitespace per entry.
    for raw_line in value.replace(",", "\n").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Each entry can still be a single token or multiple whitespace-
        # separated programs (covers the `"ls cat git"` shape).
        for token in line.split():
            cleaned = token.strip()
            if not cleaned or cleaned.startswith("#"):
                continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            out.append(cleaned)
    return tuple(out)

AGENTFLOW_DIR = Path.home() / ".agentflow"
SCOPE_FILE = AGENTFLOW_DIR / "computer-scope.toml"
AUTH_FILE = AGENTFLOW_DIR / "auth.json"

DEFAULT_WS_URL = "wss://agentflow.website/_devices/connect"


@dataclass(frozen=True)
class Scope:
    allow_apps: tuple[str, ...] = ()
    allow_paths: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = HARD_DENY_PATHS
    shell_whitelist: tuple[str, ...] = ()
    confirm_before: tuple[str, ...] = DEFAULT_CONFIRM_BEFORE
    max_actions_per_session: int = 50
    budget_usd: float = 2.0


@dataclass
class Auth:
    api_key: str = ""
    device_id: str = ""
    device_secret: str = ""
    enrollment_token: str = ""
    ws_url: str = DEFAULT_WS_URL


@dataclass
class AppConfig:
    scope: Scope = field(default_factory=Scope)
    auth: Auth = field(default_factory=Auth)


def _merge_env_shell_whitelist(file_whitelist: tuple[str, ...]) -> tuple[str, ...]:
    """Merge env-driven whitelist on top of file-driven whitelist.

    Env wins for ordering (entrypoint owns the baseline for hosted pods);
    file entries that aren't already present are appended so a user's
    `computer-scope.toml` can extend, not replace, the env baseline.
    """
    env_value = os.environ.get(SHELL_WHITELIST_ENV_VAR)
    env_entries = parse_shell_whitelist_env(env_value)
    if not env_entries:
        return file_whitelist
    if not file_whitelist:
        return env_entries
    seen = set(env_entries)
    extras = tuple(item for item in file_whitelist if item not in seen)
    return env_entries + extras


def load_scope(path: Path = SCOPE_FILE) -> Scope:
    if not path.exists():
        env_only = _merge_env_shell_whitelist(())
        if env_only:
            return Scope(shell_whitelist=env_only)
        return Scope()
    # The v0.3.x install.ps1 wrote computer-scope.toml via PowerShell's
    # `Set-Content -Encoding UTF8`, which adds a UTF-8 BOM by default.
    # tomllib chokes on the BOM with «Invalid statement (at line 1,
    # column 1)» — the daemon then crashes on every boot. Strip the
    # BOM + any leading whitespace before parsing, and on any remaining
    # parse error fall through to defaults instead of taking down the
    # daemon (user can delete the file and the GUI will write a clean
    # one).
    content = path.read_bytes()
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    text = content.decode("utf-8", "replace").lstrip()
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        print(
            f"[config] WARNING: {path} is not valid TOML ({exc}); "
            "using default scope. Delete the file to clear this warning.",
            flush=True,
        )
        raw = {}

    user_deny = tuple(raw.get("deny_paths", ()))
    merged_deny = tuple(dict.fromkeys((*HARD_DENY_PATHS, *user_deny)))

    file_whitelist = tuple(raw.get("shell_whitelist", ()))
    merged_whitelist = _merge_env_shell_whitelist(file_whitelist)

    return Scope(
        allow_apps=tuple(raw.get("allow_apps", ())),
        allow_paths=tuple(raw.get("allow_paths", ())),
        deny_paths=merged_deny,
        shell_whitelist=merged_whitelist,
        confirm_before=tuple(raw.get("confirm_before", DEFAULT_CONFIRM_BEFORE)),
        max_actions_per_session=int(raw.get("max_actions_per_session", 50)),
        budget_usd=float(raw.get("budget_usd", 2.0)),
    )


def load_auth(path: Path = AUTH_FILE) -> Auth:
    if not path.exists():
        return Auth()
    import json

    with path.open("r", encoding="utf-8") as fp:
        raw = json.load(fp)
    return Auth(
        api_key=raw.get("api_key", ""),
        device_id=raw.get("device_id", ""),
        device_secret=raw.get("device_secret", ""),
        enrollment_token=raw.get("enrollment_token", ""),
        ws_url=raw.get("ws_url", DEFAULT_WS_URL),
    )


def load_config() -> AppConfig:
    return AppConfig(scope=load_scope(), auth=load_auth())


def scope_from_mapping(raw: Mapping[str, Any] | None, base: Scope | None = None) -> Scope:
    """Build a Scope from API/WS JSON, inheriting omitted fields from `base`.

    Per-task dispatch scopes arrive as JSON objects over WS. We treat them as a
    partial override on top of the daemon's base scope so tasks can narrow or
    widen specific capabilities without losing hard-deny defaults or other
    existing settings.
    """
    if raw is None:
        return base or Scope()
    parent = base or Scope()
    return Scope(
        allow_apps=tuple(raw.get("allow_apps", parent.allow_apps)),
        allow_paths=tuple(raw.get("allow_paths", parent.allow_paths)),
        deny_paths=tuple(raw.get("deny_paths", parent.deny_paths)),
        shell_whitelist=tuple(raw.get("shell_whitelist", parent.shell_whitelist)),
        confirm_before=tuple(raw.get("confirm_before", parent.confirm_before)),
        max_actions_per_session=int(
            raw.get("max_actions_per_session", parent.max_actions_per_session)
        ),
        budget_usd=float(raw.get("budget_usd", parent.budget_usd)),
    )
