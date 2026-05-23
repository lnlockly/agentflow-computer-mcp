"""`agentflow login` / `agentflow whoami` — implementations.

The actual Typer commands live in `main.py` and call these helpers. We
keep the logic here as plain functions so tests don't fight Typer's
decorator wrapper.
"""
from __future__ import annotations

import getpass
import os

import typer

from .. import config as config_mod
from ..auth import save_auth
from ..config import AUTH_FILE, Auth, load_auth
from . import rest_client
from .format import mask_key  # noqa: F401  (re-export for tests)


def do_login(api_key: str | None) -> None:
    """Сохранить API ключ в ~/.agentflow/auth.json (mode 0600)."""
    if not api_key:
        api_key = os.environ.get("AGENTFLOW_API_KEY") or getpass.getpass("API key: ").strip()
    if not api_key:
        typer.echo("api key пустой", err=True)
        raise typer.Exit(code=2)

    existing = load_auth()
    auth = Auth(
        api_key=api_key,
        device_id=existing.device_id,
        device_secret=existing.device_secret,
        enrollment_token=existing.enrollment_token,
        ws_url=existing.ws_url,
    )
    # Re-read AUTH_FILE through the module so tests that monkeypatch
    # `config.AUTH_FILE` (or this module's AUTH_FILE) get picked up.
    target = getattr(config_mod, "AUTH_FILE", AUTH_FILE)
    save_auth(auth, path=target)
    typer.echo(f"saved {mask_key(api_key)} to {target}")


def do_whoami() -> None:
    """Показать текущего пользователя и подключённые устройства."""
    auth = load_auth()
    if not auth.api_key:
        typer.echo("не авторизован; запусти `agentflow login`", err=True)
        raise typer.Exit(code=4)

    me: object | None = None
    try:
        me = rest_client.get("/me")
    except rest_client.NotAuthenticated as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4) from exc
    except rest_client.ServerError as exc:
        # /me may not exist on every deployment; surface the local auth state
        # so the user still gets a useful answer instead of an opaque 404.
        typer.echo(f"key:     {mask_key(auth.api_key)}")
        if auth.device_id:
            typer.echo(f"device:  {auth.device_id}")
        typer.echo(f"server:  {exc.status} (endpoint /me unavailable)")
        return

    typer.echo(f"key:    {mask_key(auth.api_key)}")
    if isinstance(me, dict):
        if me.get("id") is not None:
            typer.echo(f"user:   #{me['id']}")
        if me.get("email"):
            typer.echo(f"email:  {me['email']}")
        devices = me.get("devices") or []
        typer.echo(f"devices: {len(devices)}")
        for d in devices:
            typer.echo(f"  - {d.get('id', '?')} {d.get('label', '')} {d.get('status', '')}")
