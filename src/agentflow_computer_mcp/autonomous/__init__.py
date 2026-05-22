"""Long-horizon autonomous-goal subsystem (Phase 0 — skeleton).

This package contains the scaffolding that lets the desktop agent take
a goal like «earn $1M by 2027» and turn it into a tree of milestones,
daily plans, lessons, and budget-aware sub-agent invocations.

Phase 0 ships planner + memory + daily cycle + budget tracker only.
Real money-earning workflows and multi-month coherence are explicitly
out of scope; see README §Autonomous mode for the honest cut list.
"""

from .schema import DEFAULT_DB_PATH, init_db

__all__ = ["DEFAULT_DB_PATH", "init_db"]
