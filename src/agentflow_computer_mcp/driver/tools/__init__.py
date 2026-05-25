"""Driver-side tool helpers grouped by domain.

The historical driver kept every tool implementation inline in
``desktop_tools.py``. New tools that touch multiple subsystems
(integration registry, Chrome cookies, AgentFlow API at once) live here
so each one is unit-testable in isolation.

Currently exposes:
  - ``integrations.connect_integration``: registry-driven generic
    integration connector (Track 2 of the generic-integrations spec).
  - ``project_setup.project_clone_and_setup``: clone a GitHub template
    into ``/workspace/proj-<slug>``, install deps, run dev server, and
    report the result to the backend (Phase A3 of project-architecture).
"""
from __future__ import annotations

from . import integrations, project_setup

__all__ = ["integrations", "project_setup"]
