"""Claude Code JSONL parsing (pure functions deliberately accept synthetic logs)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def parse_line(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    message = obj.get("message") or {}
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


def files(root: Path) -> Iterator[Path]:
    if root.exists():
        yield from root.glob("projects/**/*.jsonl")
