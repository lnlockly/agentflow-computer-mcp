from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HARD_DENY_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.config",
    "~/Library/Keychains",
    "~/.aws",
    "~/.gnupg",
)

DEFAULT_CONFIRM_BEFORE: tuple[str, ...] = ("computer.fs.write", "computer.shell.exec")

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
    with path.open("rb") as fp:
        raw = tomllib.load(fp)

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
