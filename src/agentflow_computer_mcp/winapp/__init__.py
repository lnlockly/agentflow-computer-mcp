"""Windows tray app — pystray-based dropdown that mirrors the Mac menu-bar.

The package is platform-agnostic enough to import on macOS/Linux for tests
(everything that touches `winreg` is wrapped). The blocking `run()` only
fires when a tray icon backend is available.

See `docs/specs/2026-05-23-windows-tray-app.md` for the design.
"""
from __future__ import annotations

__all__ = ["__version__"]

from .. import __version__  # noqa: F401  (re-export for `--version`)
