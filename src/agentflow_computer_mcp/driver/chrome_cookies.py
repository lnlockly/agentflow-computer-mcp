"""Chrome cookie exporter — reads the user's logged-in session from Chrome's
SQLite store and decrypts protected values via the OS-native keystore.

Why this beats ``document.cookie`` from ``chrome_eval``:
  - ``document.cookie`` returns only non-HttpOnly cookies. Most auth
    cookies (session tokens, CSRF) are HttpOnly and invisible to JS.
  - The shape returned here is Playwright ``storage_state.cookies``
    compatible, so downstream MCP servers (kwork-inbox, telegram-web)
    can ingest it without per-site shims.
  - Server-side ingest can run when the user's machine is asleep —
    cookies stay valid for hours/days after export.

Encryption notes (macOS):
  - macOS Chrome encrypts cookie values with AES-128-CBC. Key derivation:
    PBKDF2-HMAC-SHA1(password, salt=b"saltysalt", iter=1003, dklen=16),
    where ``password`` is the "Chrome Safe Storage" entry in Keychain.
  - The keychain entry has ``service="Chrome Safe Storage"`` and
    ``account="Chrome"``. Earlier code passed ``-a "Chrome Safe Storage"``
    which is the *account* flag — keychain returned not-found because
    no account is named that. The helper now tries the correct service
    query first and falls back to other forms for version drift.

Encryption notes (Windows):
  - Chrome's "Local State" JSON stores a DPAPI-protected AES-256-GCM key
    under ``os_crypt.encrypted_key`` (base64). The first 5 bytes after
    decode are the ``DPAPI`` magic and must be stripped.
  - Decrypt the remaining bytes via ``CryptUnprotectData`` (per-user
    DPAPI). The result is the 32-byte AES key.
  - Cookie ``encrypted_value`` rows are framed:
      bytes 0..2  = ``v10`` magic
      bytes 3..14 = 12-byte GCM nonce
      bytes 15..  = ciphertext || 16-byte GCM tag
  - The cookies DB lives at ``%LOCALAPPDATA%/Google/Chrome/User Data/
    <profile>/Network/Cookies`` (Chrome >=96) with ``<profile>/Cookies``
    as a legacy fallback.

Common notes:
  - Chrome 127+ on Windows/macOS adopted "App-Bound Encryption" with a
    ``v20`` prefix that wraps the AES key behind an OS-level service
    binding. This module returns ``error: encryption_v20_unsupported``
    for those rows; the v10 path stays fully functional.

Russian RAG-page note: docstring и комментарии на английском для grep-
ability в RAG, см. ``agentflow-code-docs/subsystems/chrome-cookies.mdx``.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Sensitive verticals — refuse to export even if technically possible. Same
# spirit as the firefox driver's host-allowlist, but applied here as a deny
# regex because the surface (cookie export) is wider than "navigate to URL".
_DENY_DOMAIN_RE = re.compile(
    r"^(.+\.)?(sberbank|tinkoff|kassa|paypal|binance|coinbase|cryptobot|qiwi|alfabank)\.(ru|com|net)$"
)

_KEYCHAIN_SERVICE = "Chrome Safe Storage"
_PBKDF2_SALT = b"saltysalt"
_PBKDF2_ITERS = 1003
_PBKDF2_DKLEN = 16
_AES_IV = b" " * 16
_MAX_COOKIES = 50

# SameSite enum from Chromium's net/cookies/cookie_constants.h. Anything
# outside this mapping → "Unspecified" (Playwright accepts that verbatim).
_SAMESITE_MAP = {
    -1: "Unspecified",
    0: "None",
    1: "Lax",
    2: "Strict",
}


def _chrome_profile_dir(profile: str) -> Path | None:
    """Return the absolute path to ``<Chrome>/<profile>/`` if it exists."""
    if sys.platform.startswith("darwin"):
        base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    elif sys.platform.startswith("linux"):
        base = Path.home() / ".config" / "google-chrome"
    elif sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        base = Path(local) / "Google" / "Chrome" / "User Data"
    else:
        return None
    candidate = base / profile
    return candidate if candidate.exists() else None


def _keychain_password() -> str | None:
    """Fetch the ``Chrome Safe Storage`` password from macOS Keychain.

    Chrome's keychain entry has ``service="Chrome Safe Storage"`` and
    ``account="Chrome"``. The first attempt uses the precise pair; the
    later attempts cover keychain entries that drift across Chrome
    versions or Brave/Edge variants where only one field is populated.

    Returns None on any failure (non-mac, locked keychain, missing entry).
    """
    if not sys.platform.startswith("darwin"):
        return None
    attempts: list[list[str]] = [
        ["security", "find-generic-password", "-w", "-s", _KEYCHAIN_SERVICE, "-a", "Chrome"],
        ["security", "find-generic-password", "-w", "-s", _KEYCHAIN_SERVICE],
        ["security", "find-generic-password", "-w", "-a", "Chrome"],
    ]
    for cmd in attempts:
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if out.returncode != 0:
            continue
        pw = out.stdout.strip()
        if pw:
            return pw
    return None


def _derive_aes_key(password: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=_PBKDF2_DKLEN,
        salt=_PBKDF2_SALT,
        iterations=_PBKDF2_ITERS,
    )
    return kdf.derive(password.encode("utf-8"))


def _decrypt_v10(encrypted: bytes, key: bytes) -> str | None:
    """Decrypt a ``v10``/``v11`` Chrome cookie value. Returns None if the
    payload fails to decrypt or unpad cleanly (corrupt row, wrong key)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    payload = encrypted[3:]  # strip "v10" / "v11" version marker
    if not payload or len(payload) % 16 != 0:
        return None
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(_AES_IV))
        decryptor = cipher.decryptor()
        padded = decryptor.update(payload) + decryptor.finalize()
    except Exception:  # noqa: BLE001 — crypto failure → drop the row
        return None
    # PKCS#7 unpad. Last byte = pad length, range 1..16.
    pad = padded[-1]
    if pad < 1 or pad > 16 or padded[-pad:] != bytes([pad]) * pad:
        return None
    try:
        return padded[:-pad].decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def _snapshot_cookie_db(src: Path) -> Path:
    """Copy the live Chrome cookie DB into a temp file. Chrome holds an
    exclusive lock on the original when running; reading the copy avoids
    ``db_locked`` for the common case where the user has Chrome open."""
    fd, tmp = tempfile.mkstemp(prefix="af-chrome-cookies-", suffix=".sqlite")
    os.close(fd)
    shutil.copy2(src, tmp)
    return Path(tmp)


def _normalize_domain(domain: str) -> str:
    """Lowercase + strip leading dot. Used both for deny-matching and for
    dedupe keys, where ``.kwork.ru`` and ``kwork.ru`` collapse to one."""
    d = domain.strip().lower()
    return d[1:] if d.startswith(".") else d


def _decrypt_v10_win_gcm(encrypted: bytes, key: bytes) -> str | None:
    """Decrypt a Windows ``v10`` Chrome cookie (AES-256-GCM)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(encrypted) < 3 + 12 + 16:
        return None
    nonce = encrypted[3:15]
    body = encrypted[15:]
    try:
        plain = AESGCM(key).decrypt(nonce, body, None)
    except Exception:  # noqa: BLE001 — crypto failure → drop the row
        return None
    try:
        return plain.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def _row_to_cookie(
    row: sqlite3.Row,
    key: bytes | None,
    decrypt: Any = None,
) -> dict[str, Any] | None:
    name = row["name"]
    plain = row["value"]
    enc = row["encrypted_value"]
    decrypt_fn = decrypt or _decrypt_v10

    if not plain and enc:
        if not enc.startswith(b"v10") and not enc.startswith(b"v11"):
            if enc.startswith(b"v20"):
                log.info("af.chrome.cookie.skip name=%s reason=v20", name[:8])
                return {"__v20__": True}
            return None
        if key is None:
            return None
        value = decrypt_fn(enc, key)
        if value is None:
            return None
    else:
        value = plain or ""

    samesite = _SAMESITE_MAP.get(row["samesite"], "Unspecified")
    # Chrome stores expires_utc as microseconds since 1601-01-01. Playwright
    # wants Unix seconds. -1 = session cookie.
    raw_exp = row["expires_utc"] or 0
    expires = -1 if raw_exp == 0 else int(raw_exp / 1_000_000 - 11_644_473_600)

    return {
        "name": name,
        "value": value,
        "domain": row["host_key"],
        "path": row["path"],
        "expires": expires,
        "httpOnly": bool(row["is_httponly"]),
        "secure": bool(row["is_secure"]),
        "sameSite": samesite,
    }


def _resolve_cookie_db(profile_dir: Path) -> Path | None:
    """Find Chrome's Cookies SQLite for a profile. Chrome >=96 on Windows
    moved the file into a ``Network/`` subdir; the legacy path stays as
    fallback for older builds and for non-Windows profiles."""
    candidates = [
        profile_dir / "Network" / "Cookies",
        profile_dir / "Cookies",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _read_cookie_rows(cookie_db: Path, norm_domain: str) -> tuple[list[sqlite3.Row], str | None]:
    """Snapshot + read the cookies table. Returns (rows, error_code)."""
    try:
        snapshot = _snapshot_cookie_db(cookie_db)
    except OSError:
        return [], "db_locked"

    rows: list[sqlite3.Row] = []
    try:
        conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT host_key, name, value, encrypted_value, path, "
                "is_secure, is_httponly, expires_utc, samesite "
                "FROM cookies WHERE host_key = ? OR host_key = ? OR host_key LIKE ?",
                (norm_domain, f".{norm_domain}", f"%.{norm_domain}"),
            )
            rows = list(cur.fetchall())
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        log.warning("af.chrome.cookie.db_err err=%s", exc)
        return [], "db_locked"
    finally:
        with contextlib.suppress(OSError):
            snapshot.unlink()
    return rows, None


def _collect_cookies(
    rows: list[sqlite3.Row],
    key: bytes | None,
    decrypt_fn: Any,
    key_missing_error: str,
) -> dict[str, Any]:
    """Walk decoded SQLite rows, decrypt, dedupe, cap. Returns the final
    result dict (success or error)."""
    seen: set[tuple[str, str, str]] = set()
    cookies: list[dict[str, Any]] = []
    v20_seen = False
    needed_key_but_missing = False
    for row in rows:
        if key is None and row["encrypted_value"] and not row["value"]:
            needed_key_but_missing = True
            continue
        parsed = _row_to_cookie(row, key, decrypt=decrypt_fn)
        if parsed is None:
            continue
        if parsed.get("__v20__"):
            v20_seen = True
            continue
        dedupe_key = (parsed["name"], parsed["domain"], parsed["path"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        cookies.append(parsed)
        if len(cookies) >= _MAX_COOKIES:
            break

    if not cookies and v20_seen:
        return {"ok": False, "error": "encryption_v20_unsupported"}
    if not cookies and needed_key_but_missing:
        return {"ok": False, "error": key_missing_error}
    return {"ok": True, "cookies": cookies}


def _export_mac(domain: str, profile: str, norm: str) -> dict[str, Any]:
    profile_dir = _chrome_profile_dir(profile)
    if profile_dir is None:
        return {"ok": False, "error": "profile_not_found"}
    cookie_db = _resolve_cookie_db(profile_dir)
    if cookie_db is None:
        return {"ok": False, "error": "profile_not_found"}

    password = _keychain_password()
    # Tests pass plaintext rows + skip keychain; the key is only required
    # when the row actually carries an ``encrypted_value``.
    key = _derive_aes_key(password) if password else None

    rows, err = _read_cookie_rows(cookie_db, norm)
    if err is not None:
        return {"ok": False, "error": err}

    collected = _collect_cookies(rows, key, _decrypt_v10, "keychain_failed")
    if not collected.get("ok"):
        return collected

    cookies = collected["cookies"]
    name_preview = ",".join(c["name"][:8] for c in cookies[:10])
    log.info("af.chrome.cookie.export domain=%s count=%d names=%s", norm, len(cookies), name_preview)
    return {
        "ok": True,
        "domain": domain,
        "profile": profile,
        "count": len(cookies),
        "cookies": cookies,
    }


def _windows_local_state_path() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    path = Path(local) / "Google" / "Chrome" / "User Data" / "Local State"
    return path if path.exists() else None


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    """Call Windows DPAPI ``CryptUnprotectData`` via ctypes. Returns the
    cleartext bytes or None on failure. No new dep — ctypes is stdlib."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return None

    class _DATA_BLOB(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    in_blob = _DATA_BLOB(len(blob), ctypes.cast(ctypes.c_char_p(blob), ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    res = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not res:
        return None
    try:
        cleartext = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return cleartext


def _windows_master_key(local_state_path: Path) -> tuple[bytes | None, str | None]:
    """Read Chrome's encrypted_key from Local State, strip DPAPI magic,
    unprotect. Returns (key, error_code)."""
    try:
        data = json.loads(local_state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "local_state_unreadable"
    try:
        encoded = data["os_crypt"]["encrypted_key"]
    except (KeyError, TypeError):
        return None, "local_state_unreadable"
    try:
        encrypted = base64.b64decode(encoded)
    except Exception:  # noqa: BLE001
        return None, "local_state_unreadable"
    if not encrypted.startswith(b"DPAPI"):
        return None, "dpapi_magic_missing"
    cleartext = _dpapi_unprotect(encrypted[5:])
    if cleartext is None:
        return None, "dpapi_failed"
    return cleartext, None


def _export_win(domain: str, profile: str, norm: str) -> dict[str, Any]:
    profile_dir = _chrome_profile_dir(profile)
    if profile_dir is None:
        return {"ok": False, "error": "profile_not_found"}
    cookie_db = _resolve_cookie_db(profile_dir)
    if cookie_db is None:
        return {"ok": False, "error": "profile_not_found"}

    local_state = _windows_local_state_path()
    key: bytes | None = None
    if local_state is not None:
        key, key_err = _windows_master_key(local_state)
        if key is None and key_err:
            log.info("af.chrome.cookie.win.key_err err=%s", key_err)

    rows, err = _read_cookie_rows(cookie_db, norm)
    if err is not None:
        return {"ok": False, "error": err}

    collected = _collect_cookies(rows, key, _decrypt_v10_win_gcm, "dpapi_failed")
    if not collected.get("ok"):
        return collected

    cookies = collected["cookies"]
    name_preview = ",".join(c["name"][:8] for c in cookies[:10])
    log.info("af.chrome.cookie.export domain=%s count=%d names=%s", norm, len(cookies), name_preview)
    return {
        "ok": True,
        "domain": domain,
        "profile": profile,
        "count": len(cookies),
        "cookies": cookies,
    }


def export_cookies(domain: str, profile: str = "Default") -> dict[str, Any]:
    """Export cookies for ``domain`` from the given Chrome profile.

    Returns a Playwright-compatible storage_state.cookies dict on success,
    or ``{"ok": False, "error": "<code>"}`` on failure. See module
    docstring for the supported error codes.
    """
    norm = _normalize_domain(domain)
    if _DENY_DOMAIN_RE.match(norm):
        log.warning("af.chrome.cookie.deny domain=%s", norm)
        return {"ok": False, "error": "domain_denied"}

    if sys.platform.startswith("darwin"):
        return _export_mac(domain, profile, norm)
    if sys.platform.startswith("win"):
        return _export_win(domain, profile, norm)
    return {"ok": False, "error": "unsupported_platform"}
