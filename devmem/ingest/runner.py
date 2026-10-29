"""Incremental, read-only ingestion of local agent logs."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ..db import connection, iso, parse_ts, project_for_cwd
from . import claude, codex, subagents

CLAUDE_LIMIT_WINDOWS = {"five_hour": 300}
RESET_TOLERANCE_S = 120  # resets derived from "resets_in_seconds" jitter by a second or two per event
log = logging.getLogger("devmem.ingest")
_FAILED: set[tuple[str, int | None, str]] = set()


class _LineFailed(Exception):
    """A line that could not be stored; carries its byte offset for the log."""
    def __init__(self, offset: int): super().__init__(offset); self.offset = offset


def _guarded(conn, path, fn, default, reset=None):
    """Run one file's ingest inside a SAVEPOINT: a bad file is rolled back, logged once, and skipped; the rest of the pass goes on."""
    conn.execute("SAVEPOINT ingest_file")
    try:
        result = fn()
    except Exception as exc:
        conn.execute("ROLLBACK TO ingest_file"); conn.execute("RELEASE ingest_file")
        if reset: reset()  # caches that may describe rolled-back rows
        if isinstance(exc, FileNotFoundError): return default  # removed between stat and open: gone, nothing to keep
        cause = exc.__cause__ if isinstance(exc, _LineFailed) and exc.__cause__ else exc
        key = (str(path), getattr(exc, "offset", None), type(cause).__name__)
        if key not in _FAILED:
            _FAILED.add(key); log.warning("ingest skipped %s at byte %s: %s: %s", path, key[1], key[2], cause)
        return default
    conn.execute("RELEASE ingest_file")
    return result


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
    """Read the status-line snapshot (written by a Claude Code status-line script); return the number of rows inserted."""
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
    count, end, seen = 0, start, set()
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
        try:
            count += _ingest_line(conn, parser, line, source, at, seen, context, store, stats)
        except Exception as exc:
            raise _LineFailed(at) from exc
    conn.execute("INSERT INTO ingest_offsets(source_file,byte_offset,context) VALUES (?,?,?) "
                 "ON CONFLICT(source_file) DO UPDATE SET byte_offset=excluded.byte_offset, context=excluded.context",
                 (source, end, json.dumps(context) if parser == "codex" and context else None))
    if parser == "codex" and isinstance(context.get("session_id"), str):
        conn.execute("UPDATE subagents SET tail_partial=? WHERE agent_id=?", (int(end < size), context["session_id"]))
    if known is not None:
        known[source] = end
    return count


def _ingest_line(conn, parser: str, line: str, source: str, at: int, seen: set, context: dict, store: LimitStore, stats: dict[str, int] | None) -> int:
    count = activities = 0
    if parser == "claude":
        event, acts = claude.parse_record(line)
        activities += _insert_activities(conn, acts, source, at)
        if event:
            key = event.pop("dedup_key")
            fresh = key == (None, None) or key not in seen
            if fresh and key != (None, None):
                seen.add(key)
                fresh = conn.execute("INSERT OR IGNORE INTO usage_dedup(agent,dedup_key) VALUES('claude',?)", (json.dumps(key),)).rowcount > 0
            if fresh:
                count += _insert_event(conn, event, source, at)
    else:
        event, limits, acts = codex.parse_record(line, context)
        rec = codex.parse_agent_record(line, context)
        if rec:
            subagents.apply_codex(conn, rec, source, at)
        activities += _insert_activities(conn, acts, source, at)
        if event:
            count += _insert_event(conn, event, source, at)
        for limit in limits:
            store.observe(limit)
    if stats is not None:
        stats["activity"] = stats.get("activity", 0) + activities
    return count


def ingest_subagent_file(conn, path: Path, known: dict[str, int] | None = None) -> bool:
    """Read new lines of one subagent log into subagents/subagent_tools, from that table's own offset.

    The offset is separate from ingest_offsets so logs already counted for tokens before v9 are still read once here.
    """
    aid = subagents.agent_id(path)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if aid is None or (known is not None and known.get(aid) == size):
        return False
    row = conn.execute("SELECT byte_offset FROM subagents WHERE agent_id=?", (aid,)).fetchone()
    start = int(row[0]) if row else 0
    if start == size:
        return False
    if size < start:
        start = 0
    subagents.ensure(conn, aid, str(path), path.parent.parent.name)
    end = start
    for at, line, next_at in _read_new(path, start):
        if line is None:
            break
        end = next_at
        try:
            rec = subagents.parse(line)
            if rec:
                subagents.apply(conn, aid, rec, at)
        except Exception as exc:
            raise _LineFailed(at) from exc
    # tail_partial: the last line is still being written, so the newest complete record may not be the run's end.
    conn.execute("UPDATE subagents SET byte_offset=?,source_file=?,tail_partial=? WHERE agent_id=?", (end, str(path), int(end < size), aid))
    if known is not None:
        known[aid] = end
    return True


def ingest_all(claude_root: Path | None = None, codex_root: Path | None = None, limits_path: Path | None = None,
               stats: dict[str, int] | None = None) -> int:
    """One incremental pass over all logs; return the number of new usage events."""
    claude_root = claude_root or Path(os.environ.get("DEVMEM_CLAUDE_HOME", "~/.claude")).expanduser()
    codex_root = codex_root or Path(os.environ.get("DEVMEM_CODEX_HOME", "~/.codex")).expanduser()
    total = 0
    with connection() as conn:
        known = {r[0]: int(r[1]) for r in conn.execute("SELECT source_file,byte_offset FROM ingest_offsets")}
        known_agents = {r[0]: int(r[1]) for r in conn.execute("SELECT agent_id,byte_offset FROM subagents")}
        store = LimitStore(conn)
        # fix(2026-10-28): every file (and each subagent step) runs in its own SAVEPOINT; one bad log never freezes usage ingest.
        _guarded(conn, claude_root, lambda: subagents.scan_meta(conn, claude_root), 0)  # first, so spawn events carry type and description
        for path in claude.files(claude_root):
            total += _guarded(conn, path, lambda: ingest_file(conn, path, "claude", known, store, stats), 0, store.last.clear)
            if subagents.agent_id(path):
                _guarded(conn, path, lambda: ingest_subagent_file(conn, path, known_agents), False)
        for path in sorted(codex.files(codex_root)):
            total += _guarded(conn, path, lambda: ingest_file(conn, path, "codex", known, store, stats), 0, store.last.clear)
        _guarded(conn, "Codex handoff links", lambda: subagents.link_codex_handoffs(conn, claude_root), None)
        _guarded(conn, "subagent events", lambda: subagents.emit_events(conn), 0)
        _guarded(conn, limits_path or claude_limits_path(), lambda: ingest_claude_limits(conn, limits_path, store), 0, store.last.clear)
    return total
