from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__
from .logging_setup import init_logging


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentflow-computer-mcp")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--mode", choices=["stdio", "ws"], default="stdio")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    if args.version:
        print(__version__)
        return 0

    init_logging(args.log_level)

    from .server import run

    try:
        asyncio.run(run(mode=args.mode))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
