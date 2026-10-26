"""Claude Code JSONL parsing (pure functions deliberately accept synthetic logs)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

SUMMARY_MAX = 120
# Only short, model-written hints are kept.  Never commands, file contents, prompts or edit strings.
SUMMARY_KEYS = ("description", "file_path", "path", "pattern")


def safe_summary(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1] + "…"
    return ""


def _usage(obj: dict[str, Any]) -> dict[str, Any] | None:
    message = obj.get("message") or {}
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("output_tokens_details") or {}
    return {
        "agent": "claude",
        "session_id": obj.get("sessionId"), "project": obj.get("cwd"),
        "model": message.get("model", "claude"), "ts": obj.get("timestamp"),
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
        "reasoning_tokens": int(details.get("thinking_tokens", 0) or 0),
        "dedup_key": (obj.get("requestId"), message.get("id")),
    }


def _activities(obj: dict[str, Any]) -> list[dict[str, Any]]:
    # Only real assistant turns: other record types (e.g. request replays) repeat old messages without timestamps.
    if obj.get("type") != "assistant":
        return []
    message = obj.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    agent = "claude-subagent" if obj.get("isSidechain") is True else "claude"
    events = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            events.append({
                "agent": agent, "session_id": obj.get("sessionId"), "project": obj.get("cwd"),
                "ts": obj.get("timestamp"), "kind": "tool", "name": str(item.get("name") or "tool")[:80],
                "summary": safe_summary(item.get("input")),
            })
    return events


def parse_record(line: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return (usage event or None, tool activity events) for one JSONL line."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None, []
    if not isinstance(obj, dict):
        return None, []
    return _usage(obj), _activities(obj)


def parse_line(line: str) -> dict[str, Any] | None:
    return parse_record(line)[0]


def files(root: Path) -> Iterator[Path]:
    if root.exists():
        yield from root.glob("projects/**/*.jsonl")
