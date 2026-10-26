"""Codex session JSONL parsing."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from ..db import iso, parse_ts

TOOL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")
TURN_TYPES = ("task_started", "task_complete")
DEFAULT_WINDOWS = {"primary": 300, "secondary": 10080}


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


def parse_line(line: str, context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return an optional usage event and rate-limit observations for one line."""
    event, limits, _ = parse_record(line, context)
    return event, limits


def files(root: Path) -> Iterator[Path]:
    if root.exists():
        yield from root.glob("sessions/**/*.jsonl")
