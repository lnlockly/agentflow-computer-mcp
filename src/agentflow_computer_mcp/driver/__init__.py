"""AgentFlow Desktop driver package.

Re-exports the public surface used by the CLI and tests.
"""
from __future__ import annotations

from .af_client import AF_TOOL_DESCRIPTORS, AFClient, dispatch_af_tool
from .desktop_tools import DESKTOP_TOOLS, PlaywrightHost, ToolExecutor, all_tool_descriptors
from .loop import build_system_prompt, run_task, task_worker
from .presets import load_presets
from .state import DriverState
from .streamer import CaptureLoop, fast_capture_jpeg
from .viewer import start_viewer

__all__ = [
    "AF_TOOL_DESCRIPTORS",
    "AFClient",
    "CaptureLoop",
    "DESKTOP_TOOLS",
    "DriverState",
    "PlaywrightHost",
    "ToolExecutor",
    "all_tool_descriptors",
    "build_system_prompt",
    "dispatch_af_tool",
    "fast_capture_jpeg",
    "load_presets",
    "run_task",
    "start_viewer",
    "task_worker",
]
