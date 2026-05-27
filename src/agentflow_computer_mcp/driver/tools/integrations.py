"""Generic, registry-driven integration connector.

This is Track 2 of the generic-integrations spec
(``docs/specs/2026-05-25-generic-integrations.md``). It replaces the
previous kwork-only prompt branch with a single ``connect_integration``
tool that asks the backend registry which provider to drive, then runs
the matching flow (cookie export via Chrome, or Telegram-app login).

Flow for ``flow_kind == 'cookie_export'``:
    1. GET ``{base}/integrations/registry`` (cached 5 min in process)
    2. Look up provider entry by slug
    3. ``chrome_open_url(login_url)`` and wait 2 s
    4. ``chrome_eval(logged_probe_js)`` — JSON shape ``{logged: bool}``
    5. If ``logged`` is False: return ``not_logged_in`` with a human
       hint asking the user to sign in and retry. Do NOT export cookies
       on a logged-out session: the export still succeeds against
       anonymous cookies and the backend wastes a write.
    6. ``chrome_export_cookies(domain=cookie_domain)`` — Playwright
       ``storage_state.cookies`` shape (HttpOnly cookies included).
    7. POST ``/me/integrations/:provider`` with ``x-api-key`` header
       and ``{cookies, profile}`` body. Backend writes the secret into
       the user's integration_hub project under
       ``provider.secret_key`` (verified by Track 1).
    8. Return a small summary: ``{ok, provider, cookie_count,
       secret_created}``.

``flow_kind == 'telegram_app'`` returns ``not_implemented`` here. Track 2
ships only the cookie path; the app-driver flow is a separate PR.

Hard rules:
    - Cookie values never appear in the returned summary. Only the
      cookie count + provider slug.
    - All dependencies (urllib opener, clock, chrome helpers) are
      injectable so tests can drive the flow without a real Chrome.
    - Module-level cache holds the registry for 5 minutes. ``slow``
      tests can bypass with ``_registry_cache_clear()``.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

REGISTRY_TTL_S = 300  # 5 minutes per spec.
DEFAULT_API_BASE = "https://agentflow.website/_agents"

# Module-level cache: {base_url: (fetched_at_monotonic, registry_list)}.
# Mutating in tests should go through ``_registry_cache_clear()``.
_REGISTRY_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _registry_cache_clear() -> None:
    """Drop the cached registry. Tests use this between assertions."""
    _REGISTRY_CACHE.clear()


def _http_get_json(
    url: str, timeout_s: int = 15, *, api_key: str | None = None
) -> Any:
    """GET ``url`` and decode JSON. Raises on non-2xx or bad JSON.

    Returned object is the parsed JSON (typically a list of provider
    dicts). HTTP errors propagate as ``urllib.error.HTTPError`` so the
    caller can map them to a tool result.

    When ``api_key`` is provided, the request carries an ``x-api-key``
    header so authenticated endpoints (``/integrations/registry``) accept
    it. Default ``None`` preserves the prior unauthenticated behaviour
    for callers that don't need it.
    """
    headers = {
        "accept": "application/json",
        "user-agent": "agentflow-desktop/0.2 (integrations)",
    }
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else None


def _http_post_json(
    url: str,
    body: dict[str, Any],
    api_key: str,
    timeout_s: int = 30,
) -> tuple[int, Any]:
    """POST JSON ``body`` with ``x-api-key`` header. Returns (status, parsed_body).

    Non-2xx responses are returned with their parsed body so the caller
    can surface backend errors (``domain_denied``, ``provider_unknown``,
    ``unauthorized``) verbatim instead of inventing a translation.
    """
    data = json.dumps(body).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "x-api-key": api_key,
        "user-agent": "agentflow-desktop/0.2 (integrations)",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else None
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed


def fetch_registry(
    api_base: str = DEFAULT_API_BASE,
    *,
    http_get: Callable[[str], Any] = _http_get_json,
    now: Callable[[], float] = time.monotonic,
    ttl_s: int = REGISTRY_TTL_S,
) -> list[dict[str, Any]]:
    """Return the provider registry, hitting the cache when fresh.

    The cache key is the API base, so daemons pointed at a staging host
    don't poison the production cache and vice versa. On HTTP / parse
    failure we raise — the caller wraps it in a ``registry_unavailable``
    tool result.
    """
    base = api_base.rstrip("/")
    cached = _REGISTRY_CACHE.get(base)
    if cached is not None:
        fetched_at, registry = cached
        if now() - fetched_at < ttl_s:
            return registry

    url = f"{base}/integrations/registry"
    payload = http_get(url)
    if isinstance(payload, dict) and "providers" in payload:
        registry = payload["providers"]
    elif isinstance(payload, list):
        registry = payload
    else:
        raise ValueError(
            f"registry response has unexpected shape: {type(payload).__name__}"
        )
    if not isinstance(registry, list):
        raise ValueError("registry providers must be a list")
    _REGISTRY_CACHE[base] = (now(), registry)
    return registry


def _find_provider(
    registry: list[dict[str, Any]], slug: str
) -> dict[str, Any] | None:
    target = slug.strip().lower()
    for entry in registry:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("slug", "")).strip().lower() == target:
            return entry
    return None


def _parse_probe_result(raw: str) -> dict[str, Any]:
    """Coerce a ``chrome_eval`` AppleScript return into a probe dict.

    AppleScript's ``execute javascript`` serialises a JS object as a
    string. Most probes (see ``prompts/integrations.py``) return a JSON
    literal already, but some hand-written ones return AppleScript-y
    bare keys (``{logged:true, url:"..."}``). We try strict JSON first,
    then fall back to a permissive parse for common shapes.
    """
    if raw is None:
        return {"logged": False, "_parse_error": "empty"}
    text = raw.strip()
    if not text:
        return {"logged": False, "_parse_error": "empty"}
    if text.startswith("error:"):
        return {"logged": False, "_parse_error": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # AppleScript-style fallback: try wrapping bare keys in quotes.
    # Cheap heuristic — only used when the probe author forgot to
    # JSON.stringify; the canonical probe in prompts/integrations.py
    # already returns a JSON object literal.
    lowered = text.lower()
    return {"logged": "true" in lowered and "logged:true" in lowered.replace(" ", "")}


def connect_integration(
    provider: str,
    *,
    api_key: str,
    api_base: str = DEFAULT_API_BASE,
    chrome_open_url: Callable[[str, bool], str],
    chrome_eval: Callable[[str, int | None], str],
    chrome_export_cookies: Callable[[str, str], dict[str, Any]],
    sleep: Callable[[float], None] = time.sleep,
    http_get: Callable[[str], Any] | None = None,
    http_post: Callable[[str, dict[str, Any], str], tuple[int, Any]] = _http_post_json,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Drive the registry-defined connect flow for ``provider``.

    Caller passes already-constructed Chrome helpers so this function
    is host-agnostic and unit-testable. On macOS the dispatcher in
    ``desktop_tools.py`` wires the real AppleScript helpers + the
    ``chrome_cookies.export_cookies`` extractor.

    Returns a small dict suitable as a tool_result JSON body. Failure
    cases all carry an ``error`` key with a stable string code so the
    UI and prompts can map them to user-facing copy.
    """
    if not provider or not isinstance(provider, str):
        return {"ok": False, "error": "provider_required"}

    # Default http_get wraps _http_get_json with the owner ``api_key`` so
    # the registry endpoint accepts the request. Tests override ``http_get``
    # with a single-arg callable; in that case we never touch the override.
    if http_get is None:
        def http_get(url: str) -> Any:
            return _http_get_json(url, api_key=api_key)

    try:
        registry = fetch_registry(
            api_base=api_base, http_get=http_get, now=now
        )
    except Exception as exc:  # noqa: BLE001 — surface as tool result
        log.warning("registry fetch failed: %s", exc)
        return {
            "ok": False,
            "error": "registry_unavailable",
            "detail": str(exc),
        }

    entry = _find_provider(registry, provider)
    if entry is None:
        return {
            "ok": False,
            "error": "provider_not_found",
            "provider": provider,
            "available": [
                str(e.get("slug")) for e in registry if isinstance(e, dict)
            ],
        }

    flow_kind = str(entry.get("flow_kind", "")).strip()
    slug = str(entry.get("slug", provider)).strip()

    if flow_kind == "telegram_app":
        return {
            "ok": False,
            "error": "not_implemented",
            "provider": slug,
            "detail": "telegram_app flow lands in a follow-up PR (Track 2b)",
        }

    if flow_kind != "cookie_export":
        return {
            "ok": False,
            "error": "unsupported_flow_kind",
            "provider": slug,
            "flow_kind": flow_kind,
        }

    login_url = str(entry.get("login_url", "")).strip()
    cookie_domain = str(entry.get("cookie_domain", "")).strip()
    probe_js = str(entry.get("logged_probe_js", "")).strip()

    if not login_url or not cookie_domain or not probe_js:
        return {
            "ok": False,
            "error": "registry_entry_incomplete",
            "provider": slug,
            "missing": [
                key
                for key, val in (
                    ("login_url", login_url),
                    ("cookie_domain", cookie_domain),
                    ("logged_probe_js", probe_js),
                )
                if not val
            ],
        }

    # Step 1+2: open the login URL and wait for the page to settle.
    try:
        open_result = chrome_open_url(login_url, True)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "chrome_open_failed",
            "provider": slug,
            "detail": str(exc),
        }
    if isinstance(open_result, str) and open_result.startswith("error:"):
        return {
            "ok": False,
            "error": "chrome_open_failed",
            "provider": slug,
            "detail": open_result,
        }
    sleep(2)

    # Step 3: probe whether the user is logged in.
    try:
        probe_raw = chrome_eval(probe_js, None)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "chrome_eval_failed",
            "provider": slug,
            "detail": str(exc),
        }
    probe = _parse_probe_result(probe_raw)
    if not probe.get("logged"):
        return {
            "ok": False,
            "error": "not_logged_in",
            "provider": slug,
            "hint": (
                f"Не вижу залогиненную сессию {entry.get('display_name', slug)} "
                "в Chrome. Открой сайт, войди, потом повтори запрос."
            ),
            "probe_url": probe.get("url"),
        }

    # Step 4: export cookies via the SQLite + Keychain path.
    try:
        export = chrome_export_cookies(cookie_domain, "Default")
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "export_failed",
            "provider": slug,
            "detail": str(exc),
        }
    if not export or not export.get("ok"):
        return {
            "ok": False,
            "error": str(export.get("error") if export else "export_failed"),
            "provider": slug,
        }
    cookies = export.get("cookies") or []
    if not cookies:
        return {
            "ok": False,
            "error": "no_cookies_exported",
            "provider": slug,
            "hint": "Chrome вернул пустой список — проверь, что юзер залогинен и сайт открыт.",
        }

    # Step 5: ship to the platform.
    post_url = f"{api_base.rstrip('/')}/me/integrations/{slug}"
    try:
        status, body = http_post(
            post_url,
            {"cookies": cookies, "profile": export.get("profile", "Default")},
            api_key,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "post_failed",
            "provider": slug,
            "detail": str(exc),
        }

    if status >= 400:
        # Pass the backend error code through verbatim (domain_denied,
        # unauthorized, provider_unknown). Cookie payload never leaks
        # back into the tool_result.
        backend_err = (
            body.get("error") if isinstance(body, dict) else None
        ) or f"http_{status}"
        return {
            "ok": False,
            "error": str(backend_err),
            "provider": slug,
            "status": status,
        }

    secret_created = False
    if isinstance(body, dict):
        secret_created = bool(
            body.get("secret_created")
            or body.get("created")
            or body.get("ok")
        )

    return {
        "ok": True,
        "provider": slug,
        "cookie_count": len(cookies),
        "secret_created": secret_created,
        "display_name": entry.get("display_name"),
    }


# Tool descriptor consumed by ``desktop_tools.all_tool_descriptors``.
CONNECT_INTEGRATION_DESCRIPTOR: dict[str, Any] = {
    "name": "connect_integration",
    "description": (
        "Connect a third-party integration (kwork, vk, lolzteam, ...). "
        "Drives the registry-defined flow: open the login URL, probe "
        "the session, export cookies, POST to "
        "/me/integrations/<provider>. Returns {ok, cookie_count, "
        "secret_created} or {ok:false, error:<code>}. Cookie values "
        "never appear in the result."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Provider slug from /integrations/registry.",
            }
        },
        "required": ["provider"],
    },
}
