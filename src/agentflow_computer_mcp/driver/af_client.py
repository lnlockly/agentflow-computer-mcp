"""Thin wrapper over the AgentFlow public REST API at https://agentflow.website/_agents.

All methods use the af_live_* API key from ~/.agentflow/auth.json. The class is sync (urllib)
to keep the driver loop simple; calls are typically ≤2s and run inside a thread already.
"""
from __future__ import annotations

import json
import time
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
    def __init__(
        self,
        api_key: str,
        base: str = DEFAULT_BASE,
        timeout_s: int = 30,
        device_id: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("AFClient requires an API key (af_live_*)")
        self._key = api_key
        self._base = base.rstrip("/")
        self._timeout = timeout_s
        # Default device id used by af_remember / af_recall when the LLM
        # doesn't pass one. Resolved from ~/.agentflow/auth.json at daemon
        # startup by the desktop_cli wiring.
        self._device_id = device_id

    @property
    def device_id(self) -> str | None:
        return self._device_id

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

    def get_project_events(
        self,
        project_id: int | str,
        since_event_id: int | str | None = None,
        limit: int = 50,
    ) -> AFResponse:
        params: dict[str, str] = {"limit": str(limit)}
        if since_event_id is not None:
            params["since_id"] = str(since_event_id)
        return self._req("GET", f"/me/projects/{project_id}/events", params=params)

    def spawn_project_task(
        self,
        brief: str,
        kind: str = "code_only",
        auto_approve: bool = True,
        poll_seconds: int = 60,
    ) -> dict[str, Any]:
        """Create + (optionally) approve a project. Polls events ≤ poll_seconds for a
        ``project_ready``-shaped event; returns whatever the project has after that.

        Returns ``{ok, project_id, slug, kind, preview_url, status, error?}``.
        """
        kind = kind or "code_only"
        body: dict[str, Any] = {"brief": brief}
        if kind:
            body["kind_hint"] = kind
        created = self._req("POST", "/me/projects", body=body)
        if not created.ok:
            return {
                "ok": False,
                "error": f"create_failed: {created.status} {created.body!r}",
            }

        proj = created.body if isinstance(created.body, dict) else {}
        project_id = proj.get("id") or proj.get("project_id")
        slug = proj.get("slug")
        if project_id is None:
            return {"ok": False, "error": f"create returned no id: {proj!r}"}

        if auto_approve:
            approved = self.approve_project(project_id)
            if not approved.ok:
                return {
                    "ok": False,
                    "project_id": project_id,
                    "slug": slug,
                    "error": f"approve_failed: {approved.status} {approved.body!r}",
                }

        deadline = time.time() + max(0, poll_seconds)
        last_status: str | None = None
        preview_url: str | None = None
        ready_kinds = {"project_ready", "preview_ready", "build_complete", "deploy_caddy"}
        while time.time() < deadline:
            ev = self.list_project_events(project_id, limit=30)
            if ev.ok and isinstance(ev.body, dict):
                items = ev.body.get("items") or ev.body.get("events") or []
                for item in items:
                    k = item.get("kind") if isinstance(item, dict) else None
                    if k in ready_kinds:
                        payload = item.get("payload") if isinstance(item, dict) else None
                        if isinstance(payload, dict):
                            preview_url = payload.get("preview_url") or preview_url
                        last_status = "ready"
                        break
                if last_status == "ready":
                    break
            time.sleep(2)

        info = self.get_project(project_id)
        if info.ok and isinstance(info.body, dict):
            preview_url = info.body.get("preview_url") or preview_url
            last_status = info.body.get("status") or last_status

        return {
            "ok": True,
            "project_id": project_id,
            "slug": slug,
            "kind": proj.get("kind") or proj.get("kind_classification") or kind,
            "preview_url": preview_url,
            "status": last_status or "pending",
        }

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

    def telegram_dialogs(self, limit: int = 25) -> AFResponse:
        """List the last N Telegram dialogs (chats / channels / DMs).

        Returns ``{ok, dialogs: [...]}``. Use instead of UI-screenshotting
        Telegram when the user asks «что у меня в TG / последние диалоги».
        """
        return self._req(
            "GET", "/me/telegram/dialogs", params={"limit": str(int(limit))}
        )

    def telegram_messages(self, chat_id: str | int, limit: int = 20) -> AFResponse:
        """Fetch the last N messages from one peer (id / @username / 'me').

        Response: ``{ok, messages: [{id, text, date, buttons, ...}]}``. Each
        row carries inline / reply keyboard buttons so the daemon can choose
        to click one back.
        """
        return self._req(
            "GET",
            "/me/telegram/messages",
            params={"chat_id": str(chat_id), "limit": str(int(limit))},
        )

    def telegram_search(
        self,
        q: str,
        chat_id: str | int | None = None,
        limit: int | None = None,
    ) -> AFResponse:
        """Search Telegram messages.

        Pass ``chat_id`` to scope inside one chat (search_messages); omit it
        for global channel/group directory search (search_telegram). Response
        shape: ``{ok, scope: 'chat'|'global', results}``.
        """
        params: dict[str, str] = {"q": q}
        if chat_id is not None and str(chat_id).strip():
            params["chat_id"] = str(chat_id)
        if limit is not None:
            params["limit"] = str(int(limit))
        return self._req("GET", "/me/telegram/search", params=params)

    def telegram_react(
        self,
        chat_id: str | int,
        message_id: int,
        emoji: str | None,
        big: bool = False,
    ) -> AFResponse:
        """Set a reaction on a Telegram message. ``emoji=None`` clears the
        existing reaction. ``big=True`` plays the full-screen animation."""
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "emoji": emoji,
        }
        if big:
            body["big"] = True
        return self._req("POST", "/me/telegram/react", body=body)

    def telegram_whoami(self) -> AFResponse:
        """Return the Telegram profile bound to the user's account (id,
        username, phone, premium). Cheap liveness probe before doing real
        work in a TG-heavy task."""
        return self._req("GET", "/me/telegram/whoami")

    def post_matrix_room(self, room_id: str, text: str) -> AFResponse:
        return self._req(
            "POST", "/me/matrix/send", body={"room_id": room_id, "text": text}
        )

    # --- Per-device memory log (af_remember / af_recall) -------------------

    def remember(
        self,
        device_id: str,
        kind: str,
        text: str,
        tags: list[str] | None = None,
    ) -> AFResponse:
        """Append one memory row for a device. `kind` is one of
        'observation' | 'lesson' | 'fact'. The autonomous loop calls this
        at task end so the next task can recall lessons by tag."""
        body: dict[str, Any] = {"kind": kind, "text": text}
        if tags:
            body["tags"] = tags
        return self._req("POST", f"/me/devices/{device_id}/memories", body=body)

    def recall(
        self,
        device_id: str,
        tags: list[str] | None = None,
        limit: int = 50,
        kind: str | None = None,
    ) -> AFResponse:
        """Fetch the newest memories for a device. `tags` is OR-matched
        (any overlap wins). `kind` filters by row kind when set."""
        params: dict[str, str] = {"limit": str(limit)}
        if tags:
            params["tags"] = ",".join(tags)
        if kind:
            params["kind"] = kind
        return self._req(
            "GET", f"/me/devices/{device_id}/memories", params=params
        )

    # --- User-editable skills (cabinet → daemon system prompt) ------------

    def get_skills_prompt_block(self, device_id: str | None = None) -> AFResponse:
        """Pre-rendered text block of the user's enabled intent skills.

        Server returns `{block: "• Когда юзер говорит «X» → instruction\\n…"}`
        or `{block: ""}` if the user has no enabled skills. The daemon
        prepends this verbatim to its system prompt so user-defined
        phrase → action mappings override the hardcoded ones.
        """
        params: dict[str, str] = {}
        if device_id:
            params["device_id"] = device_id
        return self._req("GET", "/me/devices/skills/prompt-block", params=params or None)


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
        "name": "af_telegram_dialogs",
        "description": (
            "List the user's recent Telegram dialogs (chats / channels / DMs) "
            "via MCP. Each item: id, name, username, unread count, last "
            "message preview. Use this instead of opening the Telegram app "
            "when the user asks what's in their TG."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 25},
            },
        },
    },
    {
        "name": "af_telegram_messages",
        "description": (
            "Fetch the last N messages from one Telegram peer (numeric id, "
            "@username, or 'me' for Saved Messages). Each row includes inline "
            "keyboard buttons so the daemon can decide to click one back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "af_telegram_search",
        "description": (
            "Search Telegram. Pass `chat_id` to scope the search to one "
            "chat's messages. Omit `chat_id` for global directory search "
            "across public channels and groups (Telegram's contacts.Search)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "chat_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["q"],
        },
    },
    {
        "name": "af_telegram_react",
        "description": (
            "Set a reaction emoji on a Telegram message. `emoji=null` clears "
            "the current reaction. `big=true` plays the full-screen "
            "animation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "message_id": {"type": "integer"},
                "emoji": {"type": ["string", "null"]},
                "big": {"type": "boolean", "default": False},
            },
            "required": ["chat_id", "message_id"],
        },
    },
    {
        "name": "af_telegram_whoami",
        "description": (
            "Return the Telegram profile bound to the user's account (id, "
            "username, phone, premium). Cheap liveness probe before a "
            "TG-heavy task."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "af_spawn_subagent",
        "description": (
            "Spawn a new AgentFlow project to delegate a sub-task. Creates + approves + "
            "waits ≤60s for a ready signal. Use when scope is too big for one desktop task "
            "(building a landing, a bot, a service)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief": {"type": "string"},
                "kind": {"type": "string", "default": "code_only"},
                "auto_approve": {"type": "boolean", "default": True},
            },
            "required": ["brief"],
        },
    },
    {
        "name": "af_get_project_events",
        "description": (
            "Stream progress events for a spawned project so they surface in the desktop "
            "action timeline. Pass since_event_id to page forward."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "since_event_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["project_id"],
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
    {
        "name": "af_remember",
        "description": (
            "Append one memory row for this device. Use at task end to "
            "record what worked / what did not so the next task can "
            "recall by tag. `kind` is 'observation' | 'lesson' | 'fact'. "
            "`tags` should be a short list of domain words like "
            "['kwork', 'offer'] or ['captcha', 'mail']."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": (
                        "UUID of the device this memory belongs to. "
                        "Defaults to the running daemon's device id when "
                        "omitted."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["observation", "lesson", "fact"],
                    "default": "lesson",
                },
                "text": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "required": ["kind", "text"],
        },
    },
    {
        "name": "af_recall",
        "description": (
            "Fetch this device's memories matching any of the given tags. "
            "Returns newest-first. Use at task start to recover lessons "
            "from prior runs in the same domain."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": (
                        "UUID of the device. Defaults to the running "
                        "daemon's device id when omitted."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "kind": {
                    "type": "string",
                    "enum": ["observation", "lesson", "fact"],
                },
                "limit": {"type": "integer", "default": 50},
            },
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
    elif name == "af_spawn_subagent":
        result = client.spawn_project_task(
            brief=args["brief"],
            kind=args.get("kind", "code_only"),
            auto_approve=bool(args.get("auto_approve", True)),
        )
        s = json.dumps(result, ensure_ascii=False, default=str)
        if len(s) > 4000:
            s = s[:4000] + "...<truncated>"
        return s
    elif name == "af_get_project_events":
        r = client.get_project_events(
            args["project_id"],
            since_event_id=args.get("since_event_id"),
            limit=int(args.get("limit", 50)),
        )
    elif name == "af_send_telegram_message":
        r = client.send_telegram_message(args["chat_id"], args["text"])
    elif name == "af_telegram_dialogs":
        r = client.telegram_dialogs(limit=int(args.get("limit", 25)))
    elif name == "af_telegram_messages":
        r = client.telegram_messages(
            chat_id=args["chat_id"], limit=int(args.get("limit", 20))
        )
    elif name == "af_telegram_search":
        r = client.telegram_search(
            q=args["q"],
            chat_id=args.get("chat_id"),
            limit=int(args["limit"]) if args.get("limit") is not None else None,
        )
    elif name == "af_telegram_react":
        r = client.telegram_react(
            chat_id=args["chat_id"],
            message_id=int(args["message_id"]),
            emoji=args.get("emoji"),
            big=bool(args.get("big", False)),
        )
    elif name == "af_telegram_whoami":
        r = client.telegram_whoami()
    elif name == "af_post_matrix_room":
        r = client.post_matrix_room(args["room_id"], args["text"])
    elif name == "af_remember":
        device_id = args.get("device_id") or client.device_id
        if not device_id:
            return json.dumps(
                {"ok": False, "error": "no device_id (set in auth.json or pass explicitly)"}
            )
        r = client.remember(
            device_id=device_id,
            kind=args.get("kind", "lesson"),
            text=args["text"],
            tags=args.get("tags") or [],
        )
    elif name == "af_recall":
        device_id = args.get("device_id") or client.device_id
        if not device_id:
            return json.dumps(
                {"ok": False, "error": "no device_id (set in auth.json or pass explicitly)"}
            )
        r = client.recall(
            device_id=device_id,
            tags=args.get("tags") or [],
            limit=int(args.get("limit", 50)),
            kind=args.get("kind"),
        )
    else:
        return json.dumps({"ok": False, "error": f"unknown af tool: {name}"})

    payload = {"ok": r.ok, "status": r.status, "body": r.body}
    if r.error:
        payload["error"] = r.error
    s = json.dumps(payload, ensure_ascii=False, default=str)
    if len(s) > 4000:
        s = s[:4000] + "...<truncated>"
    return s
