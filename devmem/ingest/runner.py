"""Incremental, read-only ingestion of local agent logs."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..db import connection, iso, parse_ts, project_for_cwd
from . import claude, codex

CLAUDE_LIMIT_WINDOWS = {"five_hour": 300}
RESET_TOLERANCE_S = 120  # resets derived from "resets_in_seconds" jitter by a second or two per event


def claude_limits_path() -> Path:
    return Path(os.environ.get("DEVMEM_CLAUDE_LIMITS", "~/.devmem/claude_limits.json")).expanduser()


def _read_new(path: Path, offset: int):
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


def _insert_activities(conn, events: list[dict[str, Any]], source_file: str, offset: int) -> int:
    count = 0
    # A line is far longer than its number of items, so offset+index never collides with the next line's offset.
    for index, event in enumerate(events):
        cur = conn.execute("""INSERT OR IGNORE INTO activity_events
          (agent,session_id,project,ts,kind,name,summary,source_file,source_offset) VALUES (?,?,?,?,?,?,?,?,?)""",
          (event["agent"], event.get("session_id"), project_for_cwd(event.get("project")), event.get("ts"),
           event["kind"], event.get("name"), event.get("summary") or "", source_file, offset + index))
        count += cur.rowcount > 0
    return count


class LimitStore:
    """Insert a rate_limits row only when a window's values change; otherwise just advance observed_at."""

    def __init__(self, conn):
        self.conn = conn
        self.last: dict[tuple[str, str], dict[str, Any] | None] = {}

    def _latest(self, agent: str, label: str) -> dict[str, Any] | None:
        key = (agent, label)
        if key not in self.last:
            row = self.conn.execute("SELECT id,used_percent,resets_at,window_minutes,ts,observed_at FROM rate_limits "
                                    "WHERE agent=? AND label=? ORDER BY id DESC LIMIT 1", key).fetchone()
            self.last[key] = dict(row) if row else None
        return self.last[key]

    @staticmethod
    def _same(row: dict[str, Any], obs: dict[str, Any]) -> bool:
        try:
            if float(row["used_percent"]) != float(obs["used_percent"]):
                return False
        except (TypeError, ValueError):
            if row["used_percent"] != obs["used_percent"]:
                return False
        if row["window_minutes"] != obs["window_minutes"]:
            return False
        a, b = parse_ts(row["resets_at"]), parse_ts(obs["resets_at"])
        if a is None or b is None:
            return a is b
        return abs((a - b).total_seconds()) <= RESET_TOLERANCE_S

    def observe(self, obs: dict[str, Any]) -> bool:
        """Record one observation; return True when a new row was inserted."""
        ts = iso(obs.get("ts")) or obs.get("ts")
        row = self._latest(obs["agent"], obs["label"])
        if row is not None and self._same(row, obs):
            seen = parse_ts(row.get("observed_at")) or parse_ts(row.get("ts"))
            new = parse_ts(ts)
            if new and (seen is None or new > seen):
                row["observed_at"] = ts
                self.conn.execute("UPDATE rate_limits SET observed_at=? WHERE id=?", (ts, row["id"]))
            return False
        cur = self.conn.execute("INSERT INTO rate_limits(agent,label,ts,observed_at,window_minutes,used_percent,resets_at) VALUES (?,?,?,?,?,?,?)",
                                (obs["agent"], obs["label"], ts, ts, obs["window_minutes"], obs["used_percent"], obs["resets_at"]))
        self.last[(obs["agent"], obs["label"])] = {"id": cur.lastrowid, "used_percent": obs["used_percent"], "resets_at": obs["resets_at"],
                                                   "window_minutes": obs["window_minutes"], "ts": ts, "observed_at": ts}
        return True


def _claude_window_minutes(label: str) -> int | None:
    if label in CLAUDE_LIMIT_WINDOWS:
        return CLAUDE_LIMIT_WINDOWS[label]
    if label.startswith("seven_day"):
        return 10080
    return None


def ingest_claude_limits(conn, path: Path | None = None, store: LimitStore | None = None) -> int:
    """Read the status-line snapshot (written by ~/.claude/statusline.sh); return the number of rows inserted."""
    path = path or claude_limits_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict) or not isinstance(data.get("rate_limits"), dict):
        return 0
    store = store or LimitStore(conn)
    ts = data.get("ts")
    inserted = 0
    for label, window in data["rate_limits"].items():
        if not isinstance(window, dict):
            continue
        used = window.get("used_percentage", window.get("used_percent"))
        if used is None:
            continue
        inserted += store.observe({"agent": "claude", "label": str(label), "ts": ts, "window_minutes": _claude_window_minutes(str(label)),
                                   "used_percent": used, "resets_at": iso(window.get("resets_at"))})
    return inserted


def ingest_file(conn, path: Path, parser: str, known: dict[str, int] | None = None, store: LimitStore | None = None,
                stats: dict[str, int] | None = None) -> int:
    """Ingest new complete lines of one log; return the number of new usage events."""
    source = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if known is not None and known.get(source, 0) == size:
        return 0  # unchanged since last pass: stat only, no open, no query
    row = conn.execute("SELECT byte_offset,context FROM ingest_offsets WHERE source_file=?", (source,)).fetchone()
    start = int(row[0]) if row else 0
    if start == size:
        return 0
    if size < start:
        start = 0  # truncated or rotated
    store = store or LimitStore(conn)
    count, activities, end, seen = 0, 0, start, set()
    context: dict = {}
    if parser == "codex" and start:
        try:
            context = json.loads(row[1]) if row and row[1] else {}
        except ValueError:
            context = {}
        if not context:  # offsets written by v1 carry no parser state: rebuild it once from the prefix
            with path.open("rb") as old:
                for raw in old:
                    if old.tell() > start:
                        break
                    codex.parse_record(raw.decode("utf-8", errors="replace"), context)
    for at, line, next_at in _read_new(path, start):
        if line is None:
            break
        end = next_at
        if parser == "claude":
            event, acts = claude.parse_record(line)
            activities += _insert_activities(conn, acts, source, at)
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
            event, limits, acts = codex.parse_record(line, context)
            activities += _insert_activities(conn, acts, source, at)
            if event:
                count += _insert_event(conn, event, source, at)
            for limit in limits:
                store.observe(limit)
    conn.execute("INSERT INTO ingest_offsets(source_file,byte_offset,context) VALUES (?,?,?) "
                 "ON CONFLICT(source_file) DO UPDATE SET byte_offset=excluded.byte_offset, context=excluded.context",
                 (source, end, json.dumps(context) if parser == "codex" and context else None))
    if known is not None:
        known[source] = end
    if stats is not None:
        stats["activity"] = stats.get("activity", 0) + activities
    return count


def ingest_all(claude_root: Path | None = None, codex_root: Path | None = None, limits_path: Path | None = None,
               stats: dict[str, int] | None = None) -> int:
    """One incremental pass over all logs; return the number of new usage events."""
    claude_root = claude_root or Path.home() / ".claude"
    codex_root = codex_root or Path.home() / ".codex"
    total = 0
    with connection() as conn:
        known = {r[0]: int(r[1]) for r in conn.execute("SELECT source_file,byte_offset FROM ingest_offsets")}
        store = LimitStore(conn)
        for path in claude.files(claude_root):
            total += ingest_file(conn, path, "claude", known, store, stats)
        for path in sorted(codex.files(codex_root)):
            total += ingest_file(conn, path, "codex", known, store, stats)
        ingest_claude_limits(conn, limits_path, store)
    return total
