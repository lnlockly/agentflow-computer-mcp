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


def load_scope(path: Path = SCOPE_FILE) -> Scope:
    if not path.exists():
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

    return Scope(
        allow_apps=tuple(raw.get("allow_apps", ())),
        allow_paths=tuple(raw.get("allow_paths", ())),
        deny_paths=merged_deny,
        shell_whitelist=tuple(raw.get("shell_whitelist", ())),
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
