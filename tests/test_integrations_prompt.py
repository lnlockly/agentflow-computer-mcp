"""Tests for the generic-integrations driver prompt block.

Track 4 of the generic-integrations spec (2026-05-25) replaces the
hardcoded kwork 3-step flow (chrome_open_url → chrome_eval → chrome_export_cookies)
with a single ``connect_integration(provider)`` tool. The prompt must
mention the generic tool and must NOT instruct the model to run the raw
cookie-export pipeline directly — that flow is now encapsulated in the
daemon-side tool (Track 2).
"""
from __future__ import annotations

from agentflow_computer_mcp.driver.prompts.integrations import INTEGRATIONS_BLOCK


def test_block_mentions_connect_integration_tool() -> None:
    """Model must learn the canonical generic tool name."""
    assert "connect_integration" in INTEGRATIONS_BLOCK


def test_block_drops_raw_cookie_export_flow() -> None:
    """Raw chrome_export_cookies / chrome_eval probe lives inside the
    daemon tool now. Driver prompt must not push the model to call them
    manually for integration onboarding."""
    assert "chrome_export_cookies" not in INTEGRATIONS_BLOCK
    assert "chrome_eval" not in INTEGRATIONS_BLOCK
    assert "chrome_open_url" not in INTEGRATIONS_BLOCK


def test_block_lists_known_provider_slugs() -> None:
    """Embedded registry snapshot must cover the providers from the spec."""
    for slug in ("kwork", "vk", "lolzteam", "instagram", "linkedin", "telegram_app"):
        assert slug in INTEGRATIONS_BLOCK, f"missing provider slug: {slug}"


def test_block_keeps_security_rules() -> None:
    """Security envelope must survive the refactor."""
    # No cookie-value leakage to text-block.
    assert "не выводить" in INTEGRATIONS_BLOCK.lower() or "не выводить" in INTEGRATIONS_BLOCK
    # Financial-domain deny-list mention.
    assert "domain_denied" in INTEGRATIONS_BLOCK
    # Explicit refusal pattern for "dump cookies locally" requests.
    assert "clipboard" in INTEGRATIONS_BLOCK or "файл" in INTEGRATIONS_BLOCK


def test_block_documents_not_logged_in_error() -> None:
    """The error code surfaced by the new tool when probe says logged=false
    must be documented so the model relays it verbatim."""
    assert "not_logged_in" in INTEGRATIONS_BLOCK
    assert "provider_not_found" in INTEGRATIONS_BLOCK
