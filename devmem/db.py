"""SQLite storage for devmem.  All dates are UTC ISO-8601 strings."""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2


# Version 1 schema, kept verbatim: later versions migrate from it additively.
V1_SCHEMA = """
        CREATE TABLE memories (
          id INTEGER PRIMARY KEY, project TEXT, kind TEXT NOT NULL CHECK(kind IN ('note','decision','preference','snippet','todo')),
          title TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_agent TEXT, source_session TEXT
        );
        CREATE VIRTUAL TABLE memories_fts USING fts5(title, content, tags, content='memories', content_rowid='id');
        CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
          INSERT INTO memories_fts(rowid,title,content,tags) VALUES(new.id,new.title,new.content,new.tags);
        END;
        CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
          INSERT INTO memories_fts(memories_fts,rowid,title,content,tags) VALUES('delete',old.id,old.title,old.content,old.tags);
        END;
        CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
          INSERT INTO memories_fts(memories_fts,rowid,title,content,tags) VALUES('delete',old.id,old.title,old.content,old.tags);
          INSERT INTO memories_fts(rowid,title,content,tags) VALUES(new.id,new.title,new.content,new.tags);
        END;
        CREATE TABLE agent_clients (
          id INTEGER PRIMARY KEY, client_name TEXT, client_version TEXT, pid INTEGER, cwd TEXT, project TEXT,
          started_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, ended_at TEXT
        );
        CREATE TABLE tool_calls (
          id INTEGER PRIMARY KEY, client_id INTEGER REFERENCES agent_clients(id), tool TEXT NOT NULL, args_summary TEXT,
          result_count INTEGER, duration_ms INTEGER, ok INTEGER NOT NULL, error TEXT, at TEXT NOT NULL
        );
        CREATE TABLE usage_events (
          id INTEGER PRIMARY KEY, agent TEXT NOT NULL, session_id TEXT, project TEXT, model TEXT, ts TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
          cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
          reasoning_tokens INTEGER NOT NULL DEFAULT 0, source_file TEXT NOT NULL, source_offset INTEGER NOT NULL,
          UNIQUE(source_file, source_offset)
        );
        CREATE TABLE rate_limits (
          id INTEGER PRIMARY KEY, agent TEXT NOT NULL, ts TEXT NOT NULL, window_minutes INTEGER,
          used_percent REAL, resets_at TEXT
        );
        CREATE TABLE ingest_offsets (source_file TEXT PRIMARY KEY, byte_offset INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE usage_dedup (agent TEXT NOT NULL, dedup_key TEXT NOT NULL, PRIMARY KEY(agent,dedup_key));
        CREATE INDEX memories_project_updated ON memories(project, updated_at DESC);
        CREATE INDEX usage_project_ts ON usage_events(project, ts);
        CREATE INDEX usage_session_ts ON usage_events(session_id, ts);
        CREATE INDEX tool_calls_client_at ON tool_calls(client_id, at DESC);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def db_path() -> Path:
    return Path(os.environ.get("DEVMEM_DB", "~/.devmem/devmem.db")).expanduser()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


@contextmanager
def connection(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version VALUES (0)")
        version = 0
    else:
        version = row[0]
    if version < 1:
        conn.executescript(V1_SCHEMA)
        conn.execute("UPDATE schema_version SET version=1")
    conn.commit()
    if version < 2:
        _migrate_v2(conn)


_V2_STATEMENTS = (
    # Additive only: no existing row or column is removed.
    "ALTER TABLE rate_limits ADD COLUMN label TEXT",
    # Last time the (unchanged) values were observed; rows are only inserted when values change.
    "ALTER TABLE rate_limits ADD COLUMN observed_at TEXT",
    "UPDATE rate_limits SET label = CASE window_minutes WHEN 300 THEN 'primary' WHEN 10080 THEN 'secondary' END "
    "WHERE label IS NULL AND agent='codex'",
    # Parser state (e.g. Codex session/model) so resumed files need not be re-read from byte 0.
    "ALTER TABLE ingest_offsets ADD COLUMN context TEXT",
    """CREATE TABLE activity_events (
      id INTEGER PRIMARY KEY, agent TEXT NOT NULL, session_id TEXT, project TEXT, ts TEXT,
      kind TEXT NOT NULL, name TEXT, summary TEXT,
      source_file TEXT NOT NULL, source_offset INTEGER NOT NULL,
      UNIQUE(source_file, source_offset)
    )""",
    "CREATE INDEX activity_ts ON activity_events(ts)",
    "CREATE INDEX activity_session_ts ON activity_events(session_id, ts)",
    "CREATE INDEX activity_project_ts ON activity_events(project, ts)",
    "CREATE INDEX rate_limits_agent_label ON rate_limits(agent, label, id)",
    "CREATE INDEX usage_ts ON usage_events(ts)",
)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v1 -> v2.  Runs in one IMMEDIATE transaction and re-checks the version, so concurrent processes are safe."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0] < 2:
            for statement in _V2_STATEMENTS:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version=2")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def parse_ts(value) -> datetime | None:
    """Parse the mixed timestamp formats found in logs (ISO with Z/offset/space, or unix epoch) to aware UTC."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()):
            return datetime.fromtimestamp(float(value), UTC)
        text = str(value).strip().replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def iso(value) -> str | None:
    parsed = parse_ts(value)
    return parsed.astimezone(UTC).isoformat() if parsed else None


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: quoted word tokens joined by OR (bm25 ranks multi-term hits first)."""
    return " OR ".join(f'"{token}"' for token in re.findall(r"\w+", text))


@lru_cache(maxsize=1024)
def project_for_cwd(cwd: str | None) -> str | None:
    """Return git root for cwd, without invoking git; walk for a .git marker."""
    if not cwd:
        return None
    current = Path(cwd).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return str(current)
