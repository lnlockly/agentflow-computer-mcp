from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .config import AUTH_FILE, Auth

log = logging.getLogger(__name__)


def save_auth(auth: Auth, path: Path = AUTH_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "api_key": auth.api_key,
        "device_id": auth.device_id,
        "device_secret": auth.device_secret,
        "enrollment_token": auth.enrollment_token,
        "ws_url": auth.ws_url,
    }
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, indent=2)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    log.info("auth saved to %s mode 0600", path)


def build_connect_headers(auth: Auth) -> list[tuple[str, str]]:
    if not auth.api_key:
        raise ValueError("missing api_key in auth.json")
    if not auth.device_id:
        raise ValueError("missing device_id in auth.json")

    headers: list[tuple[str, str]] = [
        ("x-api-key", auth.api_key),
        ("x-device-id", auth.device_id),
    ]
    if auth.device_secret:
        headers.append(("x-device-secret", auth.device_secret))
    elif auth.enrollment_token:
        headers.append(("x-enrollment-token", auth.enrollment_token))
    else:
        raise ValueError("auth.json has neither device_secret nor enrollment_token")
    return headers
