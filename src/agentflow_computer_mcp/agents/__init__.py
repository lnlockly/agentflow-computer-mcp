"""Multi-agent runtime.

One daemon process hosts N independent agent slots. Each slot owns:
  - its own asyncio.Queue of incoming tasks
  - its own Playwright BrowserContext (shared chromium binary, isolated cookies)
  - its own BudgetSlice (USD cap)
  - its own ~/.agentflow/agents/<id>/ dir for scope.toml + memory.db + logs

See docs/specs/2026-05-23-multi-agent-runtime.md for the design.
"""
from .budget import BudgetExhausted, BudgetSlice
from .pool import BrowserPool, PoolFull
from .router import AgentRouter
from .slot import AgentSlot, SlotStatus

__all__ = [
    "AgentRouter",
    "AgentSlot",
    "BrowserPool",
    "BudgetExhausted",
    "BudgetSlice",
    "PoolFull",
    "SlotStatus",
]
