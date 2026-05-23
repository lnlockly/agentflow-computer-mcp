"""`agentflow agent ...` — local agent slots via UNIX socket."""
from __future__ import annotations

import sys

import typer

from . import rest_client, socket_client
from .format import table

app = typer.Typer(help="Управление агентами на этой машине.", no_args_is_help=True)


def _windows_guard() -> None:
    if sys.platform == "win32":
        typer.echo("Windows: требует macOS/Linux пока #94 не выкатился", err=True)
        raise typer.Exit(code=2)


def _handle_daemon(exc: Exception) -> None:
    if isinstance(exc, socket_client.DaemonUnavailable):
        typer.echo("демон не запущен; запусти `agentflow daemon start`", err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(f"daemon error: {exc}", err=True)
    raise typer.Exit(code=1) from exc


@app.command("list")
def list_(
    remote: bool = typer.Option(False, "--remote", help="Список со всех устройств через REST"),
) -> None:
    """Показать агентов (локально по умолчанию)."""
    if remote:
        try:
            data = rest_client.get("/me/devices")
        except rest_client.ServerError as exc:
            typer.echo(f"server: {exc}", err=True)
            raise typer.Exit(code=6) from exc
        devices = (
            (data.get("devices") or data.get("items") or [])
            if isinstance(data, dict)
            else (data or [])
        )
        rows = []
        for d in devices:
            if not isinstance(d, dict):
                continue
            for a in d.get("agents", []) or []:
                if not isinstance(a, dict):
                    continue
                rows.append({
                    "device": d.get("id", "?"),
                    "id": a.get("id", ""),
                    "name": a.get("name", ""),
                    "status": a.get("status", ""),
                })
        typer.echo(table(rows, ["device", "id", "name", "status"]))
        return

    _windows_guard()
    try:
        slots = socket_client.call("list")
    except (socket_client.DaemonUnavailable, socket_client.DaemonError) as exc:
        _handle_daemon(exc)
        return
    rows = [
        {
            "id": s.get("id", ""),
            "name": s.get("name", ""),
            "persona": s.get("persona", "")[:30],
            "status": s.get("status", ""),
        }
        for s in (slots or [])
    ]
    typer.echo(table(rows, ["id", "name", "persona", "status"]))


@app.command("create")
def create(
    name: str = typer.Argument(..., help="Имя нового агента"),
    persona: str = typer.Option("", "--persona", help="Persona prompt"),
    scope: str | None = typer.Option(None, "--scope", help="Путь к scope.toml"),
) -> None:
    """Создать слот для нового агента."""
    _windows_guard()
    try:
        slot = socket_client.call("create", name=name, persona=persona, scope_path=scope)
    except (socket_client.DaemonUnavailable, socket_client.DaemonError) as exc:
        _handle_daemon(exc)
        return
    typer.echo(f"created id={slot.get('id')} name={slot.get('name')}")


def _slot_action(method: str, slot_id: str) -> None:
    _windows_guard()
    try:
        result = socket_client.call(method, id=slot_id)
    except (socket_client.DaemonUnavailable, socket_client.DaemonError) as exc:
        _handle_daemon(exc)
        return
    typer.echo(f"{method} id={result.get('id')} status={result.get('status')}")


@app.command("pause")
def pause(slot_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Поставить агента на паузу."""
    _slot_action("pause", slot_id)


@app.command("resume")
def resume(slot_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Снять агента с паузы."""
    _slot_action("resume", slot_id)


@app.command("kill")
def kill(slot_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Остановить агента (alias of pause в v1)."""
    _slot_action("pause", slot_id)


@app.command("logs")
def logs(
    slot_id: str = typer.Argument(..., metavar="ID"),
    tail: int = typer.Option(50, "--tail", "-n", help="Сколько строк показать"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Не реализовано в v1"),
) -> None:
    """Показать логи агента."""
    _windows_guard()
    if follow:
        typer.echo("--follow пока не поддерживается; используй --tail", err=True)
        raise typer.Exit(code=2)
    try:
        result = socket_client.call("logs", id=slot_id, n=tail)
    except (socket_client.DaemonUnavailable, socket_client.DaemonError) as exc:
        _handle_daemon(exc)
        return
    for line in result.get("lines", []) or []:
        typer.echo(line)
