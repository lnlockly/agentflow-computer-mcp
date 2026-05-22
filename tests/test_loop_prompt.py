"""System-prompt assertions: ensure Element / Matrix doctrine and the
cookie-onboarding flow are present in `build_system_prompt`."""
from __future__ import annotations

from agentflow_computer_mcp.driver.loop import build_system_prompt


def test_prompt_mentions_element_as_social_hub() -> None:
    p = build_system_prompt(window_summary="", af_tools_present=True)
    assert "Element" in p
    assert "chat.agentflow.website" in p
    assert "bridges" in p.lower()


def test_prompt_steers_off_native_telegram_for_social() -> None:
    p = build_system_prompt(window_summary="", af_tools_present=True)
    # Hub language must explicitly say not to open native apps.
    assert "НЕ открывай нативные приложения" in p


def test_prompt_describes_matrix_send_via_mcp() -> None:
    p = build_system_prompt(window_summary="", af_tools_present=True)
    assert "af_post_matrix_room" in p


def test_prompt_has_cookie_onboarding_flow() -> None:
    p = build_system_prompt(window_summary="", af_tools_present=True)
    assert "firefox_get_cookies" in p
    assert "firefox_export_cookies_to" in p
    # confirm-dialog language
    assert "confirm" in p.lower()


def test_prompt_existing_intent_map_kept() -> None:
    p = build_system_prompt(window_summary="", af_tools_present=True)
    # Sanity — the Element block is additive, not a replacement.
    assert "af_send_telegram_message" in p
    assert "firefox_open" in p
