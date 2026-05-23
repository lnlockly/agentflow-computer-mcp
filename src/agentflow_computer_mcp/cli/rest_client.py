"""REST client for `agentflow.website/_agents` with `x-api-key` auth.

Read order for the api key:
  1. explicit `api_key=` argument
  2. AGENTFLOW_API_KEY env
  3. ~/.agentflow/auth.json
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from ..config import load_auth

DEFAULT_BASE = "https://agentflow.website/_agents"


class NotAuthenticated(RuntimeError):
    """No api key found in flag, env, or auth.json."""


class ServerError(RuntimeError):
    """Non-2xx HTTP response."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"server returned {status}: {body[:200]}")
        self.status = status
        self.body = body


def resolve_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("AGENTFLOW_API_KEY") or os.environ.get("AF_API_KEY")
    if env:
        return env
    auth = load_auth()
    if auth.api_key:
        return auth.api_key
    raise NotAuthenticated("не авторизован; запусти `agentflow login`")


def _client(api_key: str, base: str) -> httpx.Client:
    return httpx.Client(
        base_url=base,
        headers={"x-api-key": api_key, "user-agent": "agentflow-cli/1"},
        timeout=15.0,
    )


def _check(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        raise ServerError(resp.status_code, resp.text)
    if not resp.content:
        return None
    return resp.json()


def get(path: str, *, api_key: str | None = None, base: str = DEFAULT_BASE,
        params: dict[str, Any] | None = None) -> Any:
    key = resolve_api_key(api_key)
    with _client(key, base) as c:
        return _check(c.get(path, params=params))


def post(path: str, json_body: dict[str, Any], *, api_key: str | None = None,
         base: str = DEFAULT_BASE) -> Any:
    key = resolve_api_key(api_key)
    with _client(key, base) as c:
        return _check(c.post(path, json=json_body))
