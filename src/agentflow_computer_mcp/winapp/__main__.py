"""`python -m agentflow_computer_mcp.winapp` entry point.

Subcommands:
  (no args)     — start the tray (blocks).
  install       — write Run-key entry. `--autostart` is accepted as an alias
                  for clarity in docs; behavior identical.
  uninstall     — delete the Run-key entry.
  --version     — print the package version and exit.

Kept argparse-only (no typer) so the entry point starts in ~50ms and is
safe to launch from a Windows autostart hook.
"""
from __future__ import annotations

import argparse
import sys

from .. import __version__
from . import autostart


def _cmd_install(args: argparse.Namespace) -> int:
    try:
        command = autostart.install()
    except autostart.UnsupportedPlatform as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 2
    print(f"installed autostart: {command}")
    return 0


def _cmd_uninstall(_args: argparse.Namespace) -> int:
    try:
        removed = autostart.uninstall()
    except autostart.UnsupportedPlatform as exc:
        print(f"uninstall failed: {exc}", file=sys.stderr)
        return 2
    print("uninstalled" if removed else "not installed")
    return 0


def _cmd_run(_args: argparse.Namespace) -> int:
    from . import tray

    return tray.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentflow-tray",
        description="AgentFlow Windows tray app.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")

    p_install = sub.add_parser("install", help="Зарегистрировать автозапуск")
    p_install.add_argument(
        "--autostart", action="store_true", help="Alias — поведение совпадает с install"
    )
    p_install.set_defaults(func=_cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="Убрать автозапуск")
    p_uninstall.set_defaults(func=_cmd_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)
    return _cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
