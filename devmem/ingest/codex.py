"""Codex session JSONL parsing."""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from ..db import iso, parse_ts

TOOL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")
TURN_TYPES = ("task_started", "task_complete")
DEFAULT_WINDOWS = {"primary": 300, "secondary": 10080}
TASK_MAX, REMARK_MAX, COMMAND_MAX = 2000, 500, 80
HANDOFF = re.compile(r"\b\d{8}-\d{6}-[a-z0-9]+(?:-[a-z0-9]+)*\b")


def _resets_at(limit: dict[str, Any], ts: Any) -> str | None:
    if limit.get("resets_at") is not None:
        return iso(limit.get("resets_at"))
    seconds = limit.get("resets_in_seconds")
    base = parse_ts(ts)
    if isinstance(seconds, (int, float)) and base:
        return (base + timedelta(seconds=seconds)).isoformat()
    return None


def _limits(payload: dict[str, Any], ts: Any) -> list[dict[str, Any]]:
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return []
    limit_id = rate_limits.get("limit_id")
    prefix = f"{limit_id}:" if isinstance(limit_id, str) and limit_id not in ("", "codex") else ""
    out = []
    for name, default_minutes in DEFAULT_WINDOWS.items():
        limit = rate_limits.get(name)
        if isinstance(limit, dict) and limit.get("used_percent") is not None:
            out.append({"agent": "codex", "label": prefix + name, "ts": ts,
                        "window_minutes": limit.get("window_minutes") or default_minutes,
                        "used_percent": limit.get("used_percent"), "resets_at": _resets_at(limit, ts)})
    return out


def _activity(obj: dict[str, Any], payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    kind_type = obj.get("type"), payload.get("type")
    base = {"agent": "codex", "session_id": context.get("session_id"), "project": context.get("project"), "ts": obj.get("timestamp")}
    if kind_type[0] == "response_item" and kind_type[1] in TOOL_TYPES:
        # Arguments/input hold raw commands and code: never stored.
        return {**base, "kind": "tool", "name": str(payload.get("name") or kind_type[1])[:80], "summary": ""}
    if kind_type[0] == "event_msg" and kind_type[1] in TURN_TYPES:
        summary = ""
        if kind_type[1] == "task_complete" and isinstance(payload.get("duration_ms"), (int, float)):
            summary = f"{payload['duration_ms'] / 1000:.0f}s"
        return {**base, "kind": "turn", "name": kind_type[1], "summary": summary}
    return None


def parse_record(line: str, context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (usage event or None, rate-limit observations, activity events) for one line; updates context."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None, [], []
    if not isinstance(obj, dict):
        return None, [], []
    payload = obj.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    if obj.get("type") in ("session_meta", "session_meta_event"):
        context["session_id"] = payload.get("id") or payload.get("session_id") or context.get("session_id")
        context["project"] = payload.get("cwd") or context.get("project")
    if obj.get("type") == "turn_context":
        context["model"] = payload.get("model") or context.get("model")
        context["project"] = payload.get("cwd") or context.get("project")
    activity = _activity(obj, payload, context)
    activities = [activity] if activity else []
    if obj.get("type") != "event_msg":
        return None, [], activities
    if payload.get("type") == "turn_context":
        context["model"] = payload.get("model") or context.get("model")
    usage = (payload.get("info") or {}).get("last_token_usage") if isinstance(payload.get("info"), dict) else None
    event = None
    if payload.get("type") == "token_count" and isinstance(usage, dict):
        event = {
            "agent": "codex", "session_id": context.get("session_id"), "project": context.get("project"),
            "model": context.get("model", "codex"), "ts": obj.get("timestamp"),
            "input_tokens": int(usage.get("input_tokens", usage.get("input", 0)) or 0),
            "output_tokens": int(usage.get("output_tokens", usage.get("output", 0)) or 0),
            "cache_read_tokens": int(usage.get("cached_input_tokens", usage.get("cached_input", 0)) or 0),
            "cache_write_tokens": 0,
            "reasoning_tokens": int(usage.get("reasoning_output_tokens", usage.get("reasoning_output", 0)) or 0),
        }
    return event, _limits(payload, obj.get("timestamp")), activities


def _text(content: Any) -> str | None:
    if isinstance(content, str): return content
    if isinstance(content, list):
        parts = [item.get("text") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


def _clean(text: str, size: int) -> str:
    from .subagents import clean
    return clean(text.strip(), size)


def _arguments(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("arguments", payload.get("input", payload.get("args", {})))
    if not value and isinstance(payload.get("command"), str): value = {"command": payload["command"]}
    if isinstance(value, str):
        try: value = json.loads(value)
        except ValueError: return {"command": value}
    return value if isinstance(value, dict) else {}


def _tool(payload: dict[str, Any]) -> tuple[str, str]:
    from .subagents import short_arg
    name = str(payload.get("name") or payload.get("call_id") or payload.get("type") or "tool")[:80]
    args = _arguments(payload)
    command = args.get("command") if isinstance(args.get("command"), str) else None
    if command:
        return name, _clean(" ".join(command.split()), COMMAND_MAX)
    if name == "apply_patch" or name.endswith(".apply_patch"):
        patch = args.get("patch") or args.get("input") or ""
        paths = re.findall(r"(?:\+\+\+|---) [ab]/([^\s]+)|\*\*\* Update File: ([^\n]+)|\*\*\* Add File: ([^\n]+)", patch) if isinstance(patch, str) else []
        touched = [next(part for part in match if part) for match in paths]
        return name, _clean(", ".join(dict.fromkeys(touched)), 120)
    if name.startswith("mcp__"):
        bits = name.split("__")
        return name, ".".join(bits[1:3]) if len(bits) >= 3 else name
    return name, short_arg(args)


def _is_task(text: str) -> bool:
    plain = text.lstrip()
    return bool(plain) and not (plain.startswith("<environment_context") or plain.startswith("<skills_instructions") or
                                plain.startswith("<permissions") or plain.startswith("<agents_instructions"))


def parse_agent_record(line: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """One safe, display-only Codex rollout record. parse_record owns shared context and token parsing."""
    if context.get("devmem_spawned"): return None
    try: obj = json.loads(line)
    except json.JSONDecodeError: return None
    if not isinstance(obj, dict): return None
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    typ, inner = obj.get("type"), payload.get("type")
    rec = {"session_id": context.get("session_id"), "project": context.get("project"), "model": context.get("model"),
           "ts": obj.get("timestamp"), "task": None, "text": None, "tools": [], "finished": False, "terminal": False}
    if typ == "event_msg" and inner == "task_complete": rec["finished"] = rec["terminal"] = True
    if typ != "response_item": return rec if rec["terminal"] else None
    if inner in TOOL_TYPES: rec["tools"].append(_tool(payload)); return rec
    if inner != "message": return None  # reasoning and tool output records are deliberately ignored
    role, text = payload.get("role"), _text(payload.get("content"))
    if role == "user" and isinstance(text, str) and _is_task(text):
        rec["task"] = _clean(text, TASK_MAX); return rec
    if role == "assistant" and isinstance(text, str) and text.strip():
        rec["text"] = _clean(text, REMARK_MAX)
        rec["report_text"] = _clean(text, 8000)
        return rec
    return None


def parse_line(line: str, context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return an optional usage event and rate-limit observations for one line."""
    event, limits, _ = parse_record(line, context)
    return event, limits


def files(root: Path) -> Iterator[Path]:
    if root.exists():
        yield from root.glob("sessions/**/*.jsonl")
