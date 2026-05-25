"""Single-field installer entry — `parse_token` dispatch.

The Windows wizard now accepts ONE field instead of two. The user pastes
either:

  - `af_live_<hex>` / `af_install_<hex>` — modern format, exchanged via
    REST for a fresh device row.
  - Base64url JSON blob with `{k, d, t}` — legacy invite format.

Both paths must land in `parse_token` with the same return shape so the
downstream install steps don't care which one the user pasted.
"""
from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "installer"))

from setup_gui import (  # noqa: E402  sys.path manip above
    mint_device_via_api,
    parse_invite,
    parse_token,
)


def test_parse_token_routes_af_live_to_mint() -> None:
    captured: dict = {}

    def fake_mint(api_key: str) -> dict:
        captured["api_key"] = api_key
        return {
            "api_key": api_key,
            "device_id": "dev-uuid-1",
            "device_token": "aft_xyz",
        }

    out = parse_token("af_live_abc123", mint=fake_mint)
    assert captured["api_key"] == "af_live_abc123"
    assert out["device_id"] == "dev-uuid-1"
    assert out["device_token"] == "aft_xyz"


def test_parse_token_routes_af_install_to_mint() -> None:
    captured: dict = {}

    def fake_mint(api_key: str) -> dict:
        captured["api_key"] = api_key
        return {
            "api_key": api_key,
            "device_id": "dev-uuid-2",
            "device_token": "aft_install",
        }

    out = parse_token("af_install_deadbeef", mint=fake_mint)
    assert captured["api_key"] == "af_install_deadbeef"
    assert out["device_token"] == "aft_install"


def test_parse_token_routes_legacy_blob_to_invite_parser() -> None:
    def explode_mint(*_args, **_kwargs):
        raise AssertionError("mint must not be called for legacy blob")

    # Same blob the existing smoke check uses — encodes
    # {"k":"af_live_test","d":"0000-0000-0000","t":"aft_test"}
    blob = "eyJrIjoiYWZfbGl2ZV90ZXN0IiwiZCI6IjAwMDAtMDAwMC0wMDAwIiwidCI6ImFmdF90ZXN0In0"
    out = parse_token(blob, mint=explode_mint)
    assert out == parse_invite(blob)


def test_parse_token_empty_rejected() -> None:
    with pytest.raises(ValueError, match="пуст"):
        parse_token("   ", mint=lambda *_a, **_k: {})


def test_mint_device_via_api_happy_path() -> None:
    """Verify the REST helper sends `x-api-key` and parses the response."""
    seen: dict = {}

    class FakeResp:
        def __init__(self, body: bytes) -> None:
            self._buf = BytesIO(body)

        def __enter__(self):
            return self

        def __exit__(self, *_a) -> None:
            return None

        def read(self) -> bytes:
            return self._buf.read()

    def fake_open(req, timeout):  # noqa: ARG001
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp(
            json.dumps(
                {
                    "id": "device-uuid-9",
                    "enrollment_token": "aft_minted",
                }
            ).encode("utf-8")
        )

    out = mint_device_via_api(
        "af_live_xyz",
        api_base="https://example.test/_agents",
        name="Test PC",
        opener=fake_open,
    )

    assert seen["url"] == "https://example.test/_agents/me/devices"
    assert seen["headers"]["x-api-key"] == "af_live_xyz"
    assert seen["body"] == {"name": "Test PC"}
    assert out == {
        "api_key": "af_live_xyz",
        "device_id": "device-uuid-9",
        "device_token": "aft_minted",
    }


def test_mint_device_rejects_non_af_key() -> None:
    with pytest.raises(ValueError, match="af_"):
        mint_device_via_api("nope_abc", opener=lambda *_a, **_k: None)


def test_mint_device_rejects_missing_token_in_response() -> None:
    class FakeResp:
        def __init__(self, body: bytes) -> None:
            self._buf = BytesIO(body)

        def __enter__(self):
            return self

        def __exit__(self, *_a) -> None:
            return None

        def read(self) -> bytes:
            return self._buf.read()

    def fake_open(req, timeout):  # noqa: ARG001
        return FakeResp(json.dumps({"id": "dev-1"}).encode("utf-8"))

    with pytest.raises(ValueError, match="enrollment_token"):
        mint_device_via_api(
            "af_live_xyz",
            api_base="https://example.test/_agents",
            opener=fake_open,
        )
