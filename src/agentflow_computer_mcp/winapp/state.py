"""Dataclasses for the tray's polled view of the world.

Pure data — no I/O. Producers (daemon_probe, cloud) construct these,
the menu renderer consumes them. Keeps the menu code testable without
mocking sockets and HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

DaemonStatus = Literal["up", "down", "unsupported"]


@dataclass(frozen=True)
class AgentRow:
    id: str
    name: str
    status: str  # "running", "paused", "idle", ...


@dataclass(frozen=True)
class GoalRow:
    id: str
    title: str
    status: str  # "pending", "running", "done", "paused", "failed"


@dataclass(frozen=True)
class Budget:
    spent: float = 0.0
    cap: float = 0.0


@dataclass(frozen=True)
class TrayState:
    daemon: DaemonStatus = "down"
    daemon_message: str = ""
    agents: tuple[AgentRow, ...] = field(default_factory=tuple)
    goals: tuple[GoalRow, ...] = field(default_factory=tuple)
    budget: Budget = field(default_factory=Budget)
    authenticated: bool = False

    @property
    def header(self) -> str:
        if self.daemon == "unsupported":
            return "Локальные команды требуют Windows-pipe — в работе"
        if self.daemon == "up":
            return "Подключено"
        return "Демон не запущен"
