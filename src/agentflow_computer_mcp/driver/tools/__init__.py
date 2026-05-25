"""Driver-side tool helpers grouped by domain.

The historical driver kept every tool implementation inline in
``desktop_tools.py``. New tools that touch multiple subsystems
(integration registry, Chrome cookies, AgentFlow API at once) live here
so each one is unit-testable in isolation.

Currently exposes:
  - ``integrations.connect_integration``: registry-driven generic
    integration connector (Track 2 of the generic-integrations spec).
"""
from __future__ import annotations

from . import integrations

__all__ = ["integrations"]
