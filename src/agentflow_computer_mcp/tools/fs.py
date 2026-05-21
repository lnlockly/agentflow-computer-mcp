from __future__ import annotations

from typing import Any

from ..config import Scope
from ..scope import check_path

MAX_READ_BYTES = 1_000_000


def read(path: str, scope: Scope) -> dict[str, Any]:
    target = check_path(path, scope, write=False)
    if not target.exists():
        raise FileNotFoundError(str(target))
    if not target.is_file():
        raise IsADirectoryError(str(target))

    size = target.stat().st_size
    with target.open("rb") as fp:
        data = fp.read(MAX_READ_BYTES)
    truncated = size > MAX_READ_BYTES

    try:
        content = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        import base64
        content = base64.b64encode(data).decode("ascii")
        encoding = "base64"

    return {
        "path": str(target),
        "size_bytes": size,
        "truncated": truncated,
        "encoding": encoding,
        "content": content,
    }


def list_dir(path: str, scope: Scope) -> dict[str, Any]:
    target = check_path(path, scope, write=False)
    if not target.is_dir():
        raise NotADirectoryError(str(target))

    entries = []
    for child in sorted(target.iterdir()):
        try:
            stat = child.stat()
            entries.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "size_bytes": stat.st_size if child.is_file() else None,
                "modified_ts": int(stat.st_mtime),
            })
        except OSError:
            continue

    return {"path": str(target), "entries": entries}


def write(path: str, content: str, scope: Scope, encoding: str = "utf-8") -> dict[str, Any]:
    target = check_path(path, scope, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    if encoding == "base64":
        import base64
        data = base64.b64decode(content)
        target.write_bytes(data)
    else:
        target.write_text(content, encoding="utf-8")

    return {"path": str(target), "size_bytes": target.stat().st_size}
