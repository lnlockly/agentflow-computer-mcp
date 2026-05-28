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

Flow for ``flow_kind == 'telegram_app'`` (web.telegram.org session handoff):
    1. ``chrome_open_url(login_url)`` — defaults to ``https://web.telegram.org/k/``
       if the registry entry omits it. Wait 2 s for the SPA to settle.
    2. ``chrome_eval(logged_probe_js)`` — registry-defined probe that
       inspects ``localStorage`` for ``dc1_auth_key`` / ``user_auth``.
       Defaults to a built-in probe if the registry doesn't ship one.
    3. If ``logged`` is False: return ``not_logged_in`` with a hint
       asking the user to sign in via QR / phone, then retry.
    4. ``chrome_export_cookies(web.telegram.org)`` — exports the cookies
       Telegram Web sets (``stel_ssid``, ``stel_token``, …).
    5. Optionally ``chrome_eval(local_storage_dump_js)`` to capture the
       MTProto auth keys (``dc*_auth_key``, ``user_auth``) that Telegram
       Web actually authenticates with — cookies alone are insufficient.
    6. POST ``/me/integrations/telegram`` with body
       ``{provider:'telegram', session_blob:{cookies, local_storage}}``.

This is a v1 hand-off — sufficient for owner E2E. v2 (native MTProto
``tdata`` decryption) is a separate effort tracked in the spec.

Hard rules carry over: cookie values + auth keys never appear in the
returned summary; only ``cookie_count`` and ``local_storage_keys``.

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


# Default probe + localStorage dump used when registry entry omits them.
# Telegram Web (k/ client) stores the MTProto auth key under
# `localStorage["dc1_auth_key"]` and user state under `localStorage["user_auth"]`.
# Both must be present for the session to be usable.
_DEFAULT_TELEGRAM_PROBE_JS = (
    "JSON.stringify({"
    "logged: !!(localStorage.getItem('dc1_auth_key') "
    "&& localStorage.getItem('user_auth')), "
    "url: location.href"
    "})"
)

_DEFAULT_TELEGRAM_DUMP_JS = (
    "JSON.stringify((function(){"
    "var out={};"
    "for (var i=0;i<localStorage.length;i++){"
    "var k=localStorage.key(i);"
    "if (k && (k.indexOf('dc')===0 || k==='user_auth' || k==='auth_key_id'"
    " || k==='server_time_offset' || k==='xt_instance')) {"
    "out[k]=localStorage.getItem(k);"
    "}"
    "}"
    "return out;"
    "})())"
)


def _parse_localstorage_dump(raw: str) -> dict[str, str]:
    """Coerce ``chrome_eval`` dump output into ``{key: value}`` dict.

    Returns an empty dict on parse failure rather than crashing — the
    cookies alone may still be useful for the backend to triage.
    """
    if not raw:
        return {}
    text = raw.strip()
    if not text or text.startswith("error:"):
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if v is not None}


def _run_telegram_app_flow(
    *,
    entry: dict[str, Any],
    slug: str,
    api_key: str,
    api_base: str,
    chrome_open_url: Callable[[str, bool], str],
    chrome_eval: Callable[[str, int | None], str],
    chrome_export_cookies: Callable[[str, str], dict[str, Any]],
    sleep: Callable[[float], None],
    http_post: Callable[[str, dict[str, Any], str], tuple[int, Any]],
) -> dict[str, Any]:
    """Drive the Telegram Web (web.telegram.org/k/) session hand-off.

    Isolated from the cookie_export flow so changes here can't regress
    the existing kwork/vk/lolzteam path. v1 = cookies + localStorage
    MTProto keys POSTed to ``/me/integrations/telegram``. v2 (native
    ``tdata`` decryption) is a separate effort.
    """
    login_url = (
        str(entry.get("login_url", "")).strip() or "https://web.telegram.org/k/"
    )
    cookie_domain = (
        str(entry.get("cookie_domain", "")).strip() or "web.telegram.org"
    )
    probe_js = (
        str(entry.get("logged_probe_js", "")).strip() or _DEFAULT_TELEGRAM_PROBE_JS
    )
    dump_js = (
        str(entry.get("local_storage_dump_js", "")).strip()
        or _DEFAULT_TELEGRAM_DUMP_JS
    )

    # Step 1: open Telegram Web.
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

    # Step 2: probe whether the user is logged in (localStorage check).
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
                f"Не вижу залогиненную сессию {entry.get('display_name', 'Telegram')} "
                "в Chrome. Открой web.telegram.org/k, войди по QR или номеру, "
                "потом повтори запрос."
            ),
            "probe_url": probe.get("url"),
        }

    # Step 3: export cookies (stel_ssid, stel_token, …).
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

    # Step 4: dump MTProto auth keys from localStorage. Cookies alone do
    # NOT authenticate against Telegram Web — dc*_auth_key is mandatory.
    try:
        dump_raw = chrome_eval(dump_js, None)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "chrome_eval_failed",
            "provider": slug,
            "detail": str(exc),
        }
    local_storage = _parse_localstorage_dump(dump_raw)
    if not any(k.startswith("dc") and k.endswith("_auth_key") for k in local_storage):
        return {
            "ok": False,
            "error": "no_mtproto_key",
            "provider": slug,
            "hint": (
                "В localStorage нет dc*_auth_key — Telegram Web ещё не "
                "сохранил сессию. Подожди завершения логина и повтори."
            ),
        }

    # Step 5: ship to the platform. Backend writes session_blob into the
    # user's integration_hub project under TELEGRAM_SESSION_JSON.
    post_url = f"{api_base.rstrip('/')}/me/integrations/telegram"
    session_blob = {"cookies": cookies, "local_storage": local_storage}
    try:
        status, body = http_post(
            post_url,
            {"provider": "telegram", "session_blob": session_blob},
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
        "local_storage_keys": sorted(local_storage.keys()),
        "secret_created": secret_created,
        "display_name": entry.get("display_name", "Telegram"),
    }


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
        return _run_telegram_app_flow(
            entry=entry,
            slug=slug,
            api_key=api_key,
            api_base=api_base,
            chrome_open_url=chrome_open_url,
            chrome_eval=chrome_eval,
            chrome_export_cookies=chrome_export_cookies,
            sleep=sleep,
            http_post=http_post,
        )

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


def connect_integration_direct(
    provider: str,
    *,
    api_key: str,
    api_base: str = DEFAULT_API_BASE,
    cookie_readers: list[Callable[[str], list[dict[str, Any]]]] | None = None,
    http_get: Callable[[str], Any] | None = None,
    http_post: Callable[[str, dict[str, Any], str], tuple[int, Any]] = _http_post_json,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Direct dispatch flow: skip Chrome focus / probe / AppleScript.

    Used by the WS ``task_dispatch`` short-circuit (``server._dispatch_tool``)
    so backend-driven jobs run without bringing Chrome to the front. Reads
    cookies straight from the browser SQLite stores (Chrome via Keychain,
    Safari/Arc via ``browser_cookie3``) and POSTs them to the platform.

    The first reader that returns ≥1 cookie wins. Default order tries
    Chrome (matches the LLM flow), then ``browser_cookie3.chrome``, then
    ``browser_cookie3.safari``, then ``browser_cookie3.arc``. The owner's
    manual ``python3 -c browser_cookie3.chrome(...)`` call corresponds to
    reader #2.
    """
    if not provider or not isinstance(provider, str):
        return {"ok": False, "error": "provider_required"}

    if http_get is None:
        def http_get(url: str) -> Any:
            return _http_get_json(url, api_key=api_key)

    try:
        registry = fetch_registry(api_base=api_base, http_get=http_get, now=now)
    except Exception as exc:  # noqa: BLE001
        log.warning("registry fetch failed: %s", exc)
        return {"ok": False, "error": "registry_unavailable", "detail": str(exc)}

    entry = _find_provider(registry, provider)
    if entry is None:
        return {
            "ok": False,
            "error": "provider_not_found",
            "provider": provider,
            "available": [str(e.get("slug")) for e in registry if isinstance(e, dict)],
        }

    cookie_domain = str(entry.get("cookie_domain", "")).strip()
    slug = str(entry.get("slug", provider)).strip()
    if not cookie_domain:
        return {"ok": False, "error": "registry_entry_incomplete", "provider": slug, "missing": ["cookie_domain"]}

    if cookie_readers is None:
        cookie_readers = _default_cookie_readers()

    cookies: list[dict[str, Any]] = []
    reader_used = ""
    reader_errors: list[str] = []
    for reader in cookie_readers:
        try:
            result = reader(cookie_domain)
        except Exception as exc:  # noqa: BLE001 — each reader best-effort
            reader_errors.append(f"{getattr(reader, '__name__', 'reader')}: {exc}")
            continue
        if result:
            cookies = result
            reader_used = getattr(reader, "__name__", "reader")
            break

    if not cookies:
        return {
            "ok": False,
            "error": "no_cookies_found",
            "provider": slug,
            "hint": (
                f"Не нашёл cookies для {cookie_domain} ни в одном браузере. "
                "Открой сайт в Chrome/Safari, залогинься, потом повтори."
            ),
            "reader_errors": reader_errors,
        }

    post_url = f"{api_base.rstrip('/')}/me/integrations/{slug}"
    try:
        status, body = http_post(
            post_url,
            {"cookies": cookies, "profile": reader_used},
            api_key,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "post_failed", "provider": slug, "detail": str(exc)}

    if status >= 400:
        backend_err = (body.get("error") if isinstance(body, dict) else None) or f"http_{status}"
        return {"ok": False, "error": str(backend_err), "provider": slug, "status": status}

    secret_created = False
    if isinstance(body, dict):
        secret_created = bool(body.get("secret_created") or body.get("created") or body.get("ok"))

    return {
        "ok": True,
        "provider": slug,
        "cookie_count": len(cookies),
        "browser": reader_used,
        "secret_created": secret_created,
        "display_name": entry.get("display_name"),
    }


def _default_cookie_readers() -> list[Callable[[str], list[dict[str, Any]]]]:
    """Build the default reader chain.

    Returns callables `(domain) -> list[cookie_dict]`. Chrome SQLite +
    Keychain first (matches the LLM-driven flow + handles HttpOnly via
    macOS keychain). Then ``browser_cookie3`` Chrome/Safari/Arc as
    fallbacks because owner's manual repro used browser_cookie3 directly.
    """
    readers: list[Callable[[str], list[dict[str, Any]]]] = []

    def _read_chrome_keychain(domain: str) -> list[dict[str, Any]]:
        from ..chrome_cookies import export_cookies

        result = export_cookies(domain, "Default")
        if not result or not result.get("ok"):
            return []
        return list(result.get("cookies") or [])

    _read_chrome_keychain.__name__ = "chrome_keychain"
    readers.append(_read_chrome_keychain)

    for browser_name in ("chrome", "safari", "arc", "firefox", "edge", "brave"):
        readers.append(_make_browser_cookie3_reader(browser_name))

    return readers


def _make_browser_cookie3_reader(
    browser_name: str,
) -> Callable[[str], list[dict[str, Any]]]:
    """Return a reader that calls ``browser_cookie3.<browser_name>(domain)``.

    Imported lazily so the daemon doesn't fail to start when the package
    is missing. Cookies are converted to the Playwright-compatible
    storage_state shape that the backend already accepts.
    """

    def _reader(domain: str) -> list[dict[str, Any]]:
        try:
            import browser_cookie3  # type: ignore[import-untyped]
        except ImportError:
            return []
        fn = getattr(browser_cookie3, browser_name, None)
        if fn is None:
            return []
        try:
            jar = fn(domain_name=domain)
        except Exception:  # noqa: BLE001 — browser may be locked / not installed
            return []
        out: list[dict[str, Any]] = []
        for c in jar:
            out.append(
                {
                    "name": c.name,
                    "value": c.value or "",
                    "domain": c.domain,
                    "path": c.path or "/",
                    "expires": float(c.expires) if c.expires else -1,
                    "httpOnly": bool(c._rest.get("HttpOnly") if hasattr(c, "_rest") else False),
                    "secure": bool(c.secure),
                    "sameSite": "Unspecified",
                }
            )
        return out

    _reader.__name__ = f"browser_cookie3_{browser_name}"
    return _reader


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
