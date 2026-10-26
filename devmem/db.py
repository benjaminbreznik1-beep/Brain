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

SCHEMA_VERSION = 1


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
        conn.executescript("""
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
        """)
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
    conn.commit()


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
