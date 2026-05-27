"""chrome_cookies exporter — Windows tests.

The DPAPI path can't run off-Windows, so the tests mock the bytes that
``CryptUnprotectData`` would return. Everything else (Local State parse,
profile resolution, SQLite read, AES-256-GCM decrypt, dedupe, error
codes) runs the real production code paths against fixtures.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
from pathlib import Path

from agentflow_computer_mcp.driver import chrome_cookies as cc

_CREATE_COOKIES = """
CREATE TABLE cookies (
  creation_utc INTEGER NOT NULL,
  host_key TEXT NOT NULL,
  top_frame_site_key TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  value TEXT NOT NULL,
  encrypted_value BLOB DEFAULT NULL,
  path TEXT NOT NULL,
  expires_utc INTEGER NOT NULL,
  is_secure INTEGER NOT NULL,
  is_httponly INTEGER NOT NULL,
  last_access_utc INTEGER NOT NULL,
  has_expires INTEGER NOT NULL DEFAULT 1,
  is_persistent INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 1,
  samesite INTEGER NOT NULL DEFAULT -1,
  source_scheme INTEGER NOT NULL DEFAULT 0
);
"""


def _make_profile_with_network_db(tmp_path: Path, rows: list[tuple]) -> Path:
    """Build a Windows-style profile (``Network/Cookies``) and return the
    profile dir. The cookies live in the modern subdir Chrome 96+ uses."""
    profile = tmp_path / "Default"
    (profile / "Network").mkdir(parents=True)
    db_path = profile / "Network" / "Cookies"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_CREATE_COOKIES)
        conn.executemany(
            "INSERT INTO cookies (creation_utc, host_key, name, value, encrypted_value, "
            "path, expires_utc, is_secure, is_httponly, last_access_utc, samesite) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return profile


def _encrypt_v10_gcm(value: str, key: bytes) -> bytes:
    """Produce a Windows-shaped ``v10`` ciphertext: ``v10 || nonce(12) ||
    ciphertext || tag(16)``. Mirrors Chromium's WinV10 cookie envelope."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, value.encode("utf-8"), None)
    return b"v10" + nonce + ct


def _write_local_state(tmp_path: Path, master_key: bytes) -> Path:
    """Stage a Local State JSON with a base64'd DPAPI-shaped key. The
    ``DPAPI`` prefix is the magic Chrome writes; the test patches
    ``_dpapi_unprotect`` to skip the real Win32 call."""
    local_state = tmp_path / "User Data" / "Local State"
    local_state.parent.mkdir(parents=True)
    blob = b"DPAPI" + master_key  # the body after DPAPI is the wrapped key
    payload = {"os_crypt": {"encrypted_key": base64.b64encode(blob).decode("ascii")}}
    local_state.write_text(json.dumps(payload), encoding="utf-8")
    return local_state


def test_export_win_decrypts_v10_gcm(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cc.sys, "platform", "win32")
    master_key = secrets.token_bytes(32)
    enc = _encrypt_v10_gcm("secret-win-cookie", master_key)
    rows = [
        (1, "kwork.ru", "auth", "", enc, "/", 13_300_000_000_000_000, 1, 1, 1, 1),
        (2, ".kwork.ru", "plain", "visible", None, "/", 0, 0, 0, 2, -1),
    ]
    profile = _make_profile_with_network_db(tmp_path, rows)
    local_state = _write_local_state(tmp_path, master_key)

    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: profile)
    monkeypatch.setattr(cc, "_windows_local_state_path", lambda: local_state)
    # The DPAPI body in fixture is just the raw key — short-circuit
    # ctypes by returning that body verbatim.
    monkeypatch.setattr(cc, "_dpapi_unprotect", lambda blob: blob)

    result = cc.export_cookies("kwork.ru")
    assert result["ok"] is True
    assert result["count"] == 2
    by_name = {c["name"]: c for c in result["cookies"]}
    assert by_name["auth"]["value"] == "secret-win-cookie"
    assert by_name["auth"]["httpOnly"] is True
    assert by_name["plain"]["value"] == "visible"


def test_export_win_profile_not_found(monkeypatch) -> None:
    monkeypatch.setattr(cc.sys, "platform", "win32")
    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: None)
    assert cc.export_cookies("kwork.ru") == {"ok": False, "error": "profile_not_found"}


def test_export_win_db_in_legacy_path(monkeypatch, tmp_path) -> None:
    """Pre-Chrome-96 profile has ``<profile>/Cookies`` with no Network/.
    The resolver must fall back to it."""
    monkeypatch.setattr(cc.sys, "platform", "win32")
    profile = tmp_path / "Default"
    profile.mkdir(parents=True)
    db_path = profile / "Cookies"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_CREATE_COOKIES)
        conn.execute(
            "INSERT INTO cookies (creation_utc, host_key, name, value, encrypted_value, "
            "path, expires_utc, is_secure, is_httponly, last_access_utc, samesite) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1, "kwork.ru", "t", "legacy-val", None, "/", 0, 0, 0, 1, 0),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: profile)
    monkeypatch.setattr(cc, "_windows_local_state_path", lambda: None)
    result = cc.export_cookies("kwork.ru")
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["cookies"][0]["value"] == "legacy-val"


def test_export_win_dpapi_failure_yields_dpapi_failed(monkeypatch, tmp_path) -> None:
    """If DPAPI returns None (locked profile, wrong user) but rows are
    encrypted, the export should not pretend to succeed."""
    monkeypatch.setattr(cc.sys, "platform", "win32")
    master_key = secrets.token_bytes(32)
    enc = _encrypt_v10_gcm("never-decrypted", master_key)
    rows = [(1, "kwork.ru", "auth", "", enc, "/", 0, 1, 1, 1, 0)]
    profile = _make_profile_with_network_db(tmp_path, rows)
    local_state = _write_local_state(tmp_path, master_key)

    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: profile)
    monkeypatch.setattr(cc, "_windows_local_state_path", lambda: local_state)
    monkeypatch.setattr(cc, "_dpapi_unprotect", lambda blob: None)

    result = cc.export_cookies("kwork.ru")
    assert result == {"ok": False, "error": "dpapi_failed"}


def test_export_win_domain_denied_pre_platform(monkeypatch) -> None:
    """The deny-list short-circuits before any platform code runs, so
    even with Windows mocked nothing leaks for blocked domains."""
    monkeypatch.setattr(cc.sys, "platform", "win32")
    result = cc.export_cookies("sberbank.ru")
    assert result == {"ok": False, "error": "domain_denied"}


def test_windows_master_key_strips_dpapi_magic(monkeypatch, tmp_path) -> None:
    """``_windows_master_key`` must base64-decode, verify the ``DPAPI``
    magic, strip it, and forward the rest to ``_dpapi_unprotect``."""
    master_key = b"\x42" * 32
    captured: dict[str, bytes] = {}

    def fake_unprotect(blob: bytes) -> bytes:
        captured["blob"] = blob
        return master_key

    monkeypatch.setattr(cc, "_dpapi_unprotect", fake_unprotect)
    local_state = _write_local_state(tmp_path, master_key)

    key, err = cc._windows_master_key(local_state)
    assert err is None
    assert key == master_key
    # The bytes handed to DPAPI must be the post-magic body — i.e. the
    # raw master key — not the original DPAPI-prefixed envelope.
    assert captured["blob"] == master_key


def test_windows_master_key_rejects_missing_magic(monkeypatch, tmp_path) -> None:
    """An ``encrypted_key`` lacking the ``DPAPI`` prefix is corrupt; the
    helper must surface ``dpapi_magic_missing`` rather than calling DPAPI."""
    local_state = tmp_path / "Local State"
    bogus = base64.b64encode(b"NOTDPAPI" + b"\x00" * 32).decode("ascii")
    local_state.write_text(json.dumps({"os_crypt": {"encrypted_key": bogus}}), encoding="utf-8")

    called = {"n": 0}

    def fake_unprotect(_blob):  # noqa: ANN001
        called["n"] += 1
        return b""

    monkeypatch.setattr(cc, "_dpapi_unprotect", fake_unprotect)
    key, err = cc._windows_master_key(local_state)
    assert key is None
    assert err == "dpapi_magic_missing"
    assert called["n"] == 0


def test_windows_local_state_path_uses_localappdata(monkeypatch, tmp_path) -> None:
    target = tmp_path / "Google" / "Chrome" / "User Data" / "Local State"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert cc._windows_local_state_path() == target


def test_windows_local_state_path_missing_env(monkeypatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert cc._windows_local_state_path() is None


def test_chrome_profile_dir_windows(monkeypatch, tmp_path) -> None:
    base = tmp_path / "Google" / "Chrome" / "User Data" / "Default"
    base.mkdir(parents=True)
    monkeypatch.setattr(cc.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert cc._chrome_profile_dir("Default") == base


def test_other_unix_still_unsupported(monkeypatch) -> None:
    """Linux remains unsupported until we add a libsecret path."""
    monkeypatch.setattr(cc.sys, "platform", "linux")
    assert cc.export_cookies("kwork.ru") == {"ok": False, "error": "unsupported_platform"}


# Touch unused imports so the linter doesn't complain in slimmer suites.
_ = os
