"""Codex session JSONL parsing."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def parse_line(line: str, context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return an optional usage event and rate-limit observations for one line."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None, []
    payload = obj.get("payload") or {}
    if obj.get("type") == "session_meta" or obj.get("type") == "session_meta_event":
        meta = payload if isinstance(payload, dict) else {}
        context["session_id"] = meta.get("id") or meta.get("session_id") or context.get("session_id")
        context["project"] = meta.get("cwd") or context.get("project")
    if obj.get("type") == "turn_context":
        context["model"] = payload.get("model") or context.get("model")
    if obj.get("type") != "event_msg":
        return None, []
    if payload.get("type") == "turn_context":
        context["model"] = payload.get("model") or context.get("model")
    usage = (payload.get("info") or {}).get("last_token_usage")
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
    limits: list[dict[str, Any]] = []
    for name, minutes in (("primary", 300), ("secondary", 10080)):
        limit = (payload.get("rate_limits") or {}).get(name)
        if isinstance(limit, dict):
            limits.append({"agent": "codex", "ts": obj.get("timestamp"), "window_minutes": minutes,
                           "used_percent": limit.get("used_percent"), "resets_at": limit.get("resets_at")})
    return event, limits


def files(root: Path) -> Iterator[Path]:
    if root.exists():
        yield from root.glob("sessions/**/*.jsonl")
