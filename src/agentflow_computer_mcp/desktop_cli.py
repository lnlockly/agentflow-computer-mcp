"""Entry point for the AgentFlow Desktop daemon.

One process serves: HTTP viewer (port 8765) + MJPEG capture loop + LLM task
worker + optional WS reverse-tunnel to AgentFlow prod (so cabinet-side
`dispatch_task` reaches the same DriverState as local chat input).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_auth, load_config
from .driver import (
    AFClient,
    CaptureLoop,
    DriverState,
    PlaywrightHost,
    ToolExecutor,
    load_presets,
    start_viewer,
    task_worker,
)
from .driver.desktop_tools import grab_full_png
from .driver.loop import DEFAULT_LLM_URL, DEFAULT_MODEL, run_task

log = logging.getLogger(__name__)


def _resolve_api_key(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    env = os.environ.get("AGENTFLOW_API_KEY") or os.environ.get("AF_API_KEY")
    if env:
        return env
    auth = load_auth()
    return auth.api_key or ""


def _start_ws_bridge(
    state: DriverState,
    capture: CaptureLoop,
) -> tuple[threading.Thread, Any] | None:
    """Spin up the WS reverse-tunnel in a sidecar thread sharing DriverState.

    Returns (thread, ws_client) or None when the auth file isn't enrolled.
    The thread owns its own asyncio loop; it stays alive for the process
    lifetime and reconnects on its own.
    """
    from .server import TOOL_NAMES, _dispatch_tool
    from .ws_client import WSClient

    config = load_config()
    if not config.auth.api_key or not config.auth.device_id:
        log.info("ws bridge skipped: ~/.agentflow/auth.json not enrolled")
        return None
    if not config.auth.device_secret and not config.auth.enrollment_token:
        log.info("ws bridge skipped: no device_secret / enrollment_token in auth.json")
        return None

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return await _dispatch_tool(name, args, config)

    def on_task_dispatch(task_id: str, task: str, scope: dict[str, Any] | None) -> None:
        log.info("ws task_dispatch id=%s task=%s", task_id, task[:80])
        state.enqueue_task(task, task_id)

    def on_stream_subscribe(subscribe: bool) -> None:
        if subscribe:
            state.stream_subscribed.set()
        else:
            state.stream_subscribed.clear()
        log.info("ws stream subscribed=%s", subscribe)

    client = WSClient(
        config,
        handler,
        TOOL_NAMES,
        on_task_dispatch=on_task_dispatch,
        on_stream_subscribe=on_stream_subscribe,
    )

    # Bind the outbound publisher into DriverState (for task_action /
    # task_complete frames from the AI loop) and into the capture loop
    # (for stream_frame frames).
    state.outbound_publisher = client.publish
    capture.set_outbound_publisher(client.publish)

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(client.run())
        finally:
            loop.close()

    thread = threading.Thread(target=_run, name="ws-bridge", daemon=True)
    thread.start()
    return thread, client


def cmd_run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    api_key = _resolve_api_key(args.api_key)
    if not api_key:
        print(
            "no API key found. set AGENTFLOW_API_KEY or ~/.agentflow/auth.json or pass --api-key",
            file=sys.stderr,
        )
        return 2

    state = DriverState()
    presets = load_presets(Path(args.presets) if args.presets else None)
    log.info("loaded %d presets", len(presets))

    # warm Quartz on main thread before background capture starts
    try:
        grab_full_png()
    except Exception as exc:  # noqa: BLE001
        log.warning("screen warmup failed: %s (continuing)", exc)

    capture = CaptureLoop(
        state.stream_frame,
        state.stream_cond,
        fps=args.fps,
        stream_subscribed=state.stream_subscribed,
    )
    capture.start()
    server = start_viewer(state, presets, port=args.port, host=args.host)
    log.info("live viewer: http://%s:%d", args.host, args.port)

    bridge: tuple[threading.Thread, Any] | None = None
    if not args.no_ws:
        bridge = _start_ws_bridge(state, capture)

    print(
        f"\n{'=' * 70}\n"
        f"AgentFlow Desktop {__version__}\n"
        f"  viewer:  http://{args.host}:{args.port}\n"
        f"  model:   {args.model}\n"
        f"  presets: {len(presets)}\n"
        f"  ws:      {'on' if bridge else 'off (enroll via /me/devices to enable)'}\n"
        f"{'=' * 70}\n",
        flush=True,
    )

    af = AFClient(api_key) if not args.no_af_tools else None
    executor = ToolExecutor(state.last_cursor, af_client=af, pw=PlaywrightHost())

    shutdown = threading.Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("received signal %d, shutting down", signum)
        shutdown.set()
        if bridge is not None:
            _, client = bridge
            with contextlib.suppress(Exception):
                client.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):
            signal.signal(sig, _on_signal)

    try:
        task_worker(
            state,
            executor,
            api_key,
            llm_url=args.llm_url,
            model=args.model,
        )
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        capture.stop()
        server.shutdown()
        if bridge is not None:
            _, client = bridge
            with contextlib.suppress(Exception):
                client.stop()
    return 0


def cmd_drive(args: argparse.Namespace) -> int:
    """One-shot: run a single task without viewer/queue."""
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    api_key = _resolve_api_key(args.api_key)
    if not api_key:
        print("no API key found", file=sys.stderr)
        return 2

    state = DriverState()
    af = AFClient(api_key) if not args.no_af_tools else None
    executor = ToolExecutor(state.last_cursor, af_client=af, pw=PlaywrightHost())
    answer = run_task(args.task, state, executor, api_key, llm_url=args.llm_url, model=args.model)
    print(answer)
    return 0


def cmd_version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def cmd_selftest(_: argparse.Namespace) -> int:
    """Probe each backend capability and print an OK/FAIL grid.

    No network, no API keys, no LLM. Use this to verify a fresh install on any OS.
    """
    from collections.abc import Callable

    from .platform import PLATFORM, backend

    print(f"platform: {PLATFORM}")
    if backend is None:
        print("FAIL: no backend for this platform")
        return 1
    print(f"backend:  {backend.name}\n")

    checks: list[tuple[str, str]] = []

    def _run(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            checks.append((label, "OK"))
        except Exception as exc:  # noqa: BLE001
            checks.append((label, f"FAIL: {exc}"))

    _run("capture_screen_fast", lambda: backend.capture_screen_fast())
    _run("capture_screen (png)", lambda: backend.capture_screen())
    _run("window_list", lambda: backend.window_list())
    _run("clipboard_read", lambda: backend.clipboard_read())
    _run("read_terminal (optional)", lambda: backend.read_terminal())

    width = max(len(name) for name, _ in checks)
    failed = 0
    for name, status in checks:
        print(f"  {name.ljust(width)}  {status}")
        if status.startswith("FAIL"):
            failed += 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 0 if failed == 0 else 1


def cmd_tools(args: argparse.Namespace) -> int:
    """List the tools the daemon exposes to the LLM."""
    from .driver.desktop_tools import all_tool_descriptors

    for t in all_tool_descriptors():
        print(f"  {t['name']:32s}  {t['description']}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Probe that capture + AF API work."""
    api_key = _resolve_api_key(args.api_key)
    if not api_key:
        print("FAIL: no API key", file=sys.stderr)
        return 2
    try:
        grab_full_png()
        print("OK: screen capture")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: screen capture: {exc}", file=sys.stderr)
        return 1
    try:
        af = AFClient(api_key)
        r = af.list_devices()
        if r.ok:
            count = len(r.body.get("items", [])) if isinstance(r.body, dict) else 0
            print(f"OK: AF API reachable ({count} devices)")
        else:
            print(f"FAIL: AF API status={r.status} body={r.body}", file=sys.stderr)
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: AF API: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentflow-desktop",
        description="AgentFlow Desktop — local Mac driver + MJPEG viewer + AgentFlow API tools",
    )
    p.add_argument("--version", action="store_true", help="print version and exit")
    sub = p.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="start the daemon (viewer + worker + ws bridge)")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("--fps", type=int, default=20)
    run.add_argument("--api-key", default=None, help="overrides AGENTFLOW_API_KEY / auth.json")
    run.add_argument("--llm-url", default=DEFAULT_LLM_URL)
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--presets", default=None, help="path to preset yaml")
    run.add_argument("--no-af-tools", action="store_true", help="hide af_* tools from LLM")
    run.add_argument(
        "--no-ws",
        action="store_true",
        help="disable the cabinet WS reverse-tunnel (viewer/worker only)",
    )
    run.add_argument("--log-level", default="INFO")
    run.set_defaults(func=cmd_run)

    drive = sub.add_parser("drive", help="run a single task once (no viewer)")
    drive.add_argument("task")
    drive.add_argument("--api-key", default=None)
    drive.add_argument("--llm-url", default=DEFAULT_LLM_URL)
    drive.add_argument("--model", default=DEFAULT_MODEL)
    drive.add_argument("--no-af-tools", action="store_true")
    drive.add_argument("--log-level", default="INFO")
    drive.set_defaults(func=cmd_drive)

    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)
    sub.add_parser("tools", help="list LLM-facing tools").set_defaults(func=cmd_tools)
    sub.add_parser(
        "selftest",
        help="probe screen capture + window list + clipboard on this OS",
    ).set_defaults(func=cmd_selftest)

    health = sub.add_parser("health", help="probe capture + AF API")
    health.add_argument("--api-key", default=None)
    health.set_defaults(func=cmd_health)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
