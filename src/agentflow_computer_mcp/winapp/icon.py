"""Load the tray icon as a PIL Image with HiDPI fallbacks.

If a bundled PNG is missing we synthesise a flat-colour 32x32 square so
dev installs without the asset still light up the tray.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

ASSETS_DIR = Path(__file__).parent / "assets"
ASSET_SIZES = (16, 32, 48)


def _synthetic(size: int = 32) -> Any:
    from PIL import Image

    return Image.new("RGB", (size, size), color=(108, 71, 255))  # AgentFlow violet


def load_icon() -> Any:
    """Return the largest available PNG as a PIL.Image. Falls back to a solid square."""
    from PIL import Image

    for size in reversed(ASSET_SIZES):
        path = ASSETS_DIR / f"logo-{size}.png"
        if path.exists():
            try:
                return Image.open(path)
            except Exception:
                continue
    return _synthetic()
