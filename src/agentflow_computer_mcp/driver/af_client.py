"""Thin wrapper over the AgentFlow public REST API at https://agentflow.website/_agents.

All methods use the af_live_* API key from ~/.agentflow/auth.json. The class is sync (urllib)
to keep the driver loop simple; calls are typically ≤2s and run inside a thread already.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE = "https://agentflow.website/_agents"


@dataclass
class AFResponse:
    ok: bool
    status: int
    body: Any
    error: str | None = None


class AFClient:
    def __init__(self, api_key: str, base: str = DEFAULT_BASE, timeout_s: int = 30) -> None:
        if not api_key:
            raise ValueError("AFClient requires an API key (af_live_*)")
        self._key = api_key
        self._base = base.rstrip("/")
        self._timeout = timeout_s

    def _req(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> AFResponse:
        url = self._base + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "x-api-key": self._key,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "agentflow-desktop/0.2",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                parsed = json.loads(raw) if raw else None
                return AFResponse(ok=True, status=resp.status, body=parsed)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            return AFResponse(ok=False, status=exc.code, body=parsed, error=str(exc))
        except urllib.error.URLError as exc:
            return AFResponse(ok=False, status=0, body=None, error=str(exc))

    # --- Projects ---------------------------------------------------------

    def list_projects(self, limit: int = 20) -> AFResponse:
        return self._req("GET", "/me/projects", params={"limit": str(limit)})

    def get_project(self, project_id: int | str) -> AFResponse:
        return self._req("GET", f"/me/projects/{project_id}")

    def create_project(self, brief: str) -> AFResponse:
        return self._req("POST", "/me/projects", body={"brief": brief})

    def approve_project(self, project_id: int | str) -> AFResponse:
        return self._req("POST", f"/me/projects/{project_id}/approve", body={})

    def list_project_events(self, project_id: int | str, limit: int = 20) -> AFResponse:
        return self._req(
            "GET", f"/me/projects/{project_id}/events", params={"limit": str(limit)}
        )

    # --- Devices ----------------------------------------------------------

    def list_devices(self) -> AFResponse:
        return self._req("GET", "/me/devices")

    def get_device(self, device_id: str) -> AFResponse:
        return self._req("GET", f"/me/devices/{device_id}")

    # --- Agents -----------------------------------------------------------

    def list_agents(self, limit: int = 20) -> AFResponse:
        return self._req("GET", "/me/agents", params={"limit": str(limit)})

    def send_agent_message(self, agent_id: int | str, text: str) -> AFResponse:
        return self._req(
            "POST", f"/me/agents/{agent_id}/message", body={"text": text}
        )

    # --- Telegram / Matrix (optional surfaces) -----------------------------

    def send_telegram_message(self, chat_id: str | int, text: str) -> AFResponse:
        return self._req(
            "POST", "/me/telegram/send", body={"chat_id": chat_id, "text": text}
        )

    def post_matrix_room(self, room_id: str, text: str) -> AFResponse:
        return self._req(
            "POST", "/me/matrix/send", body={"room_id": room_id, "text": text}
        )


# Tool descriptors for the LLM (Anthropic tool schema). These mirror AFClient methods
# 1:1 so the driver can advertise them in the system prompt.
AF_TOOL_DESCRIPTORS: list[dict[str, Any]] = [
    {
        "name": "af_list_projects",
        "description": "List the user's AgentFlow projects (id, slug, title, kind).",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "af_get_project",
        "description": "Fetch one project by id, including preview_url + status.",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "af_create_project",
        "description": "Create a new AgentFlow project from a free-form brief. Returns id+slug+kind.",
        "input_schema": {
            "type": "object",
            "properties": {"brief": {"type": "string"}},
            "required": ["brief"],
        },
    },
    {
        "name": "af_approve_project",
        "description": "Approve a draft project so the build flow starts.",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "af_list_project_events",
        "description": "Last N events of a project (build/coder/preview lifecycle).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "af_list_devices",
        "description": "List the user's registered desktop devices.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "af_get_device",
        "description": "Fetch one device by UUID.",
        "input_schema": {
            "type": "object",
            "properties": {"device_id": {"type": "string"}},
            "required": ["device_id"],
        },
    },
    {
        "name": "af_list_agents",
        "description": "List the user's AgentFlow agents (marketplace + project-bound).",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "af_send_agent_message",
        "description": "Send a chat message to one of the user's AgentFlow agents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["agent_id", "text"],
        },
    },
    {
        "name": "af_send_telegram_message",
        "description": "Send a Telegram message via the user's bound Telegram channel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["chat_id", "text"],
        },
    },
    {
        "name": "af_post_matrix_room",
        "description": "Post a message into a Matrix room via the user's bound Matrix account.",
        "input_schema": {
            "type": "object",
            "properties": {
                "room_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["room_id", "text"],
        },
    },
]


def dispatch_af_tool(client: AFClient, name: str, args: dict[str, Any]) -> str:
    """Execute one af_* tool. Returns a JSON string (≤4KB) safe for LLM tool_result."""
    if name == "af_list_projects":
        r = client.list_projects(limit=int(args.get("limit", 20)))
    elif name == "af_get_project":
        r = client.get_project(args["project_id"])
    elif name == "af_create_project":
        r = client.create_project(args["brief"])
    elif name == "af_approve_project":
        r = client.approve_project(args["project_id"])
    elif name == "af_list_project_events":
        r = client.list_project_events(args["project_id"], limit=int(args.get("limit", 20)))
    elif name == "af_list_devices":
        r = client.list_devices()
    elif name == "af_get_device":
        r = client.get_device(args["device_id"])
    elif name == "af_list_agents":
        r = client.list_agents(limit=int(args.get("limit", 20)))
    elif name == "af_send_agent_message":
        r = client.send_agent_message(args["agent_id"], args["text"])
    elif name == "af_send_telegram_message":
        r = client.send_telegram_message(args["chat_id"], args["text"])
    elif name == "af_post_matrix_room":
        r = client.post_matrix_room(args["room_id"], args["text"])
    else:
        return json.dumps({"ok": False, "error": f"unknown af tool: {name}"})

    payload = {"ok": r.ok, "status": r.status, "body": r.body}
    if r.error:
        payload["error"] = r.error
    s = json.dumps(payload, ensure_ascii=False, default=str)
    if len(s) > 4000:
        s = s[:4000] + "...<truncated>"
    return s
