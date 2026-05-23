"""`agentflow daemon status/start/stop`."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import typer

from . import socket_client

app = typer.Typer(help="Управление локальным демоном агентов.", no_args_is_help=True)

PID_FILE = Path.home() / ".agentflow" / "daemon.pid"


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@app.command("status")
def status() -> None:
    """Запущен ли локальный демон?"""
    if sys.platform == "win32":
        typer.echo("Windows: демон пока недоступен (см. #94)")
        raise typer.Exit(code=2)

    pid = _read_pid()
    socket_ok = Path(socket_client.DEFAULT_SOCKET_PATH).exists()

    if pid and _is_running(pid):
        typer.echo(f"running pid={pid} socket={'ok' if socket_ok else 'missing'}")
        return
    if socket_ok:
        try:
            socket_client.call("list")
        except socket_client.DaemonUnavailable:
            typer.echo("not running (stale socket)")
            raise typer.Exit(code=1) from None
        typer.echo("running (no pid file)")
        return
    typer.echo("not running")
    raise typer.Exit(code=1)


@app.command("start")
def start(
    detach: bool = typer.Option(True, "--detach/--foreground", help="Запустить в фоне"),
) -> None:
    """Запустить локальный демон (agentflow-desktop)."""
    if sys.platform == "win32":
        typer.echo("Windows: демон пока недоступен (см. #94)", err=True)
        raise typer.Exit(code=2)

    pid = _read_pid()
    if pid and _is_running(pid):
        typer.echo(f"already running pid={pid}")
        return

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["agentflow-desktop"]
    if detach:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        typer.echo(f"started pid={proc.pid}")
    else:
        os.execvp(cmd[0], cmd)


@app.command("stop")
def stop() -> None:
    """Остановить локальный демон."""
    if sys.platform == "win32":
        typer.echo("Windows: демон пока недоступен (см. #94)", err=True)
        raise typer.Exit(code=2)

    pid = _read_pid()
    if not pid or not _is_running(pid):
        typer.echo("not running")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        typer.echo(f"kill failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    PID_FILE.unlink(missing_ok=True)
    typer.echo(f"stopped pid={pid}")
