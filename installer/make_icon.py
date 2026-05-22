"""Generate the setup .exe icon (lime "AF" on black).

Runs at build time inside the GitHub Action. Produces
`installer/build_assets/agentflow.ico` with multiple resolutions.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def build_icon(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 32, 48, 64, 128, 256]
    images: list[Image.Image] = []
    for size in sizes:
        img = Image.new("RGBA", (size, size), (11, 11, 15, 255))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arialbd.ttf", int(size * 0.6))
        except OSError:
            font = ImageFont.load_default()
        text = "AF"
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = (size - w) // 2 - bbox[0]
        y = (size - h) // 2 - bbox[1]
        draw.text((x, y), text, fill=(166, 242, 92, 255), font=font)
        images.append(img)
    images[0].save(out_path, format="ICO", sizes=[(s, s) for s in sizes])


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "build_assets" / "agentflow.ico"
    build_icon(target)
    print(f"wrote {target}")
