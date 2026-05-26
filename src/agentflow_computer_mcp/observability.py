"""GlitchTip / Sentry init for the hosted daemon.

The cluster runs a self-hosted GlitchTip at ``glitchtip.agentflow.website``
(Sentry-compatible ingest). When ``SENTRY_DSN`` is set in the pod env,
this module wires the SDK so:

  * Unhandled exceptions in the daemon Python process (WS frame handler,
    MCP tool dispatch, asyncio tasks) auto-report with a stack + tags.
  * ``report_event(message, level, **tags)`` lets the daemon log
    structured events without raising — useful for "opencode died at
    startup" or "ws bridge dropped".
  * Tags include ``device_id``, ``user_id``, ``hosted_device_id``,
    ``pod_name``, ``image_sha`` so issues filter cleanly per pod.

Safe no-op when ``SENTRY_DSN`` is empty: local dev, selftest, and CI
runs don't ship traces.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_ENABLED = False


def init_sentry() -> None:
    """Initialise sentry_sdk once at process start.

    Reads env: ``SENTRY_DSN``, ``SENTRY_ENVIRONMENT`` (default 'production'),
    ``SENTRY_RELEASE`` (default the daemon version), plus pod/device tags
    from ``AF_DEVICE_ID``, ``AF_HOSTED_DEVICE_ID``, ``HOSTNAME``.

    Safe to call multiple times — second call is a no-op.
    """
    global _ENABLED
    if _ENABLED:
        return
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        # sentry-sdk is a runtime dep but tolerate missing for tests that
        # import this module without installing extras.
        log.warning("sentry_sdk not installed; SENTRY_DSN ignored")
        return

    environment = os.environ.get("SENTRY_ENVIRONMENT", "production")
    release = os.environ.get("SENTRY_RELEASE") or _read_version()

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        # 5% of healthy traces sampled to keep volume sane; errors always
        # send regardless of this rate.
        traces_sample_rate=0.05,
        # Capture print() / logging.* output as breadcrumbs.
        attach_stacktrace=True,
        send_default_pii=False,
    )
    # Pod / device context.
    sentry_sdk.set_tag("pod_name", os.environ.get("HOSTNAME", "unknown"))
    if device_id := os.environ.get("AF_DEVICE_ID"):
        sentry_sdk.set_tag("device_id", device_id)
    if hosted_id := os.environ.get("AF_HOSTED_DEVICE_ID"):
        sentry_sdk.set_tag("hosted_device_id", hosted_id)
    if image_sha := os.environ.get("AF_IMAGE_SHA"):
        sentry_sdk.set_tag("image_sha", image_sha)

    _ENABLED = True
    log.info("[sentry] init ok env=%s release=%s", environment, release)


def is_enabled() -> bool:
    return _ENABLED


def report_event(message: str, level: str = "info", **tags: Any) -> None:
    """Send a structured event to GlitchTip without raising.

    Safe no-op when Sentry is disabled. Used by ``agent_dev_brief`` to
    surface opencode startup failures, by ``ws_client`` for connection
    lifecycle, etc.
    """
    if not _ENABLED:
        return
    try:
        import sentry_sdk
    except ImportError:
        return
    with sentry_sdk.push_scope() as scope:
        for k, v in tags.items():
            scope.set_tag(k, str(v)[:200])
        sentry_sdk.capture_message(message, level=level)


def _read_version() -> str:
    """Best-effort daemon version from package metadata."""
    try:
        from importlib.metadata import version

        return version("agentflow-computer-mcp")
    except Exception:  # noqa: BLE001
        return "unknown"
