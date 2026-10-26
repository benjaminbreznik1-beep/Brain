"""Incremental, read-only ingestion of local agent logs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..db import connection, project_for_cwd
from . import claude, codex


def _read_new(path: Path, offset: int):
    size = path.stat().st_size
    if size < offset:
        offset = 0
    with path.open("rb") as f:
        f.seek(offset)
        while True:
            at = f.tell()
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                yield at, None, at  # leave an unfinished final line for next pass
                break
            yield at, raw.decode("utf-8", errors="replace"), f.tell()


def _insert_event(conn, event, source_file: str, offset: int) -> bool:
    event["project"] = project_for_cwd(event.get("project"))  # logs carry the raw cwd; group by git root
    values = [event.get(k) for k in ("agent", "session_id", "project", "model", "ts", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")]
    cur = conn.execute("""INSERT OR IGNORE INTO usage_events
      (agent,session_id,project,model,ts,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,source_file,source_offset)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (*values, source_file, offset))
    return cur.rowcount > 0


def ingest_file(conn, path: Path, parser: str) -> int:
    source = str(path)
    row = conn.execute("SELECT byte_offset FROM ingest_offsets WHERE source_file=?", (source,)).fetchone()
    start = int(row[0]) if row else 0
    count, end, seen = 0, start, set()
    context: dict = {}
    if parser == "codex" and start:
        with path.open("rb") as old:
            for raw in old:
                if old.tell() > start:
                    break
                codex.parse_line(raw.decode("utf-8", errors="replace"), context)
    for at, line, next_at in _read_new(path, start):
        if line is None:
            break
        end = next_at
        if parser == "claude":
            event = claude.parse_line(line)
            if event:
                key = event.pop("dedup_key")
                if key != (None, None) and key in seen:
                    continue
                seen.add(key)
                if key != (None, None):
                    if conn.execute("INSERT OR IGNORE INTO usage_dedup(agent,dedup_key) VALUES('claude',?)", (json.dumps(key),)).rowcount == 0:
                        continue
                count += _insert_event(conn, event, source, at)
        else:
            event, limits = codex.parse_line(line, context)
            if event:
                count += _insert_event(conn, event, source, at)
            for limit in limits:
                conn.execute("INSERT INTO rate_limits(agent,ts,window_minutes,used_percent,resets_at) VALUES (?,?,?,?,?)",
                             tuple(limit[k] for k in ("agent", "ts", "window_minutes", "used_percent", "resets_at")))
    conn.execute("INSERT INTO ingest_offsets(source_file,byte_offset) VALUES (?,?) ON CONFLICT(source_file) DO UPDATE SET byte_offset=excluded.byte_offset", (source, end))
    return count


def ingest_all(claude_root: Path | None = None, codex_root: Path | None = None) -> int:
    claude_root = claude_root or Path.home() / ".claude"
    codex_root = codex_root or Path.home() / ".codex"
    total = 0
    with connection() as conn:
        for path in claude.files(claude_root): total += ingest_file(conn, path, "claude")
        for path in codex.files(codex_root): total += ingest_file(conn, path, "codex")
    return total
