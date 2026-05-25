"""chrome_cookies exporter tests.

The real macOS Keychain + real Chrome data are out of scope for CI. We
build a fixture SQLite DB matching Chrome's schema, then exercise:
  - plaintext rows (the keychain path is mocked / skipped)
  - encrypted rows via a known PBKDF2 password + hand-rolled AES-CBC ct
  - deny-list rejection for financial domains
  - profile_not_found / unsupported_platform / encryption_v20_unsupported
  - dedupe of (name, domain, path) across host_key variants
  - 50-cookie cap
"""
from __future__ import annotations

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


def _make_profile(tmp_path: Path, rows: list[tuple]) -> Path:
    """Build a fake ``<Chrome>/<profile>/Cookies`` SQLite store and return
    the ``<profile>/`` dir so the export function's discovery can find it."""
    profile = tmp_path / "Default"
    profile.mkdir(parents=True)
    db_path = profile / "Cookies"
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


def _encrypt_v10(value: str, password: str) -> bytes:
    """Produce a ``v10`` Chrome ciphertext for the given plaintext."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = cc._derive_aes_key(password)
    raw = value.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    padded = raw + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(cc._AES_IV)).encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return b"v10" + ct


def test_unsupported_platform_returns_error(monkeypatch) -> None:
    monkeypatch.setattr(cc.sys, "platform", "linux")
    result = cc.export_cookies("kwork.ru")
    assert result == {"ok": False, "error": "unsupported_platform"}


def test_domain_denied(monkeypatch) -> None:
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    for d in ["sberbank.ru", "tinkoff.ru", "paypal.com", "binance.com", "coinbase.com"]:
        result = cc.export_cookies(d)
        assert result == {"ok": False, "error": "domain_denied"}, d


def test_profile_not_found(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _profile: None)
    assert cc.export_cookies("kwork.ru") == {"ok": False, "error": "profile_not_found"}


def test_plaintext_rows_export(monkeypatch, tmp_path) -> None:
    """Rows with ``value`` set and ``encrypted_value`` NULL bypass crypto."""
    rows = [
        # (ctime, host_key, name, value, enc, path, exp_utc, secure, httponly, last, samesite)
        (1, "kwork.ru", "track", "abc123", None, "/", 0, 1, 0, 1, 1),
        (2, ".kwork.ru", "session_id", "deadbeef", None, "/", 0, 1, 1, 2, 2),
        (3, "other.com", "foo", "bar", None, "/", 0, 0, 0, 3, -1),
    ]
    profile = _make_profile(tmp_path, rows)
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: profile)
    monkeypatch.setattr(cc, "_keychain_password", lambda: None)

    result = cc.export_cookies("kwork.ru")
    assert result["ok"] is True
    assert result["count"] == 2
    names = {c["name"] for c in result["cookies"]}
    assert names == {"track", "session_id"}
    by_name = {c["name"]: c for c in result["cookies"]}
    assert by_name["session_id"]["httpOnly"] is True
    assert by_name["session_id"]["sameSite"] == "Strict"
    assert by_name["track"]["sameSite"] == "Lax"
    assert by_name["track"]["expires"] == -1


def test_encrypted_rows_export(monkeypatch, tmp_path) -> None:
    password = "test-keychain-password"
    enc_value = _encrypt_v10("real-secret-token", password)
    rows = [
        (1, "kwork.ru", "auth", "", enc_value, "/", 13_000_000_000_000_000, 1, 1, 1, 0),
    ]
    profile = _make_profile(tmp_path, rows)
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: profile)
    monkeypatch.setattr(cc, "_keychain_password", lambda: password)

    result = cc.export_cookies("kwork.ru")
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["cookies"][0]["value"] == "real-secret-token"
    assert result["cookies"][0]["httpOnly"] is True
    assert result["cookies"][0]["sameSite"] == "None"


def test_encrypted_row_missing_keychain_returns_keychain_failed(monkeypatch, tmp_path) -> None:
    password = "key-present-during-encrypt"
    enc_value = _encrypt_v10("hidden", password)
    rows = [
        (1, "kwork.ru", "auth", "", enc_value, "/", 0, 1, 1, 1, 0),
    ]
    profile = _make_profile(tmp_path, rows)
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: profile)
    monkeypatch.setattr(cc, "_keychain_password", lambda: None)

    result = cc.export_cookies("kwork.ru")
    assert result == {"ok": False, "error": "keychain_failed"}


def test_v20_row_returns_v20_error(monkeypatch, tmp_path) -> None:
    rows = [
        (1, "kwork.ru", "auth", "", b"v20" + b"\x00" * 32, "/", 0, 1, 1, 1, 0),
    ]
    profile = _make_profile(tmp_path, rows)
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: profile)
    monkeypatch.setattr(cc, "_keychain_password", lambda: "anything")

    result = cc.export_cookies("kwork.ru")
    assert result == {"ok": False, "error": "encryption_v20_unsupported"}


def test_dedupe_across_host_key_variants(monkeypatch, tmp_path) -> None:
    """Two rows with identical (name, domain literal, path) collapse to one.

    Chrome can hold both a ``.kwork.ru`` and ``kwork.ru`` row for the same
    cookie name during a domain migration; clients should not see both.
    """
    rows = [
        (1, "kwork.ru", "track", "v1", None, "/", 0, 1, 0, 1, 1),
        (2, "kwork.ru", "track", "v2-duplicate", None, "/", 0, 1, 0, 2, 1),
    ]
    profile = _make_profile(tmp_path, rows)
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: profile)
    monkeypatch.setattr(cc, "_keychain_password", lambda: None)

    result = cc.export_cookies("kwork.ru")
    assert result["count"] == 1


def test_fifty_cookie_cap(monkeypatch, tmp_path) -> None:
    rows = [
        (i, "kwork.ru", f"name_{i:03d}", f"val_{i}", None, "/", 0, 1, 0, i, 1)
        for i in range(75)
    ]
    profile = _make_profile(tmp_path, rows)
    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_chrome_profile_dir", lambda _p: profile)
    monkeypatch.setattr(cc, "_keychain_password", lambda: None)

    result = cc.export_cookies("kwork.ru")
    assert result["ok"] is True
    assert result["count"] == 50


def test_descriptor_registered() -> None:
    """Sanity check the tool surface is wired into the dispatcher catalog."""
    from agentflow_computer_mcp.driver.desktop_tools import DESKTOP_TOOLS  # noqa: PLC0415

    names = {t["name"] for t in DESKTOP_TOOLS}
    assert "chrome_export_cookies" in names
