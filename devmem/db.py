"""SQLite storage for devmem.  All dates are UTC ISO-8601 strings."""
from __future__ import annotations

import os
import re
import sqlite3
import hashlib
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import numpy as np

from .embed import EMBEDDING_DIM, EMBEDDING_MODEL, get_embedder

SCHEMA_VERSION = 12
DEFAULT_MAX_CONTENT = 2000
# bge-small-en-v1.5 scores unrelated text ~0.45-0.59 and real matches ~0.65+ (measured on ~40 memories, 2026-10).
MIN_COSINE = 0.6
# measured 2026-10-27 on 56 memories: real duplicates scored 0.86-0.92, related-but-distinct memories up to 0.89, so this is a hint, not a block.
SIMILAR_COSINE = 0.80


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


def max_content() -> int:
    """Return the configured content cap, falling back to a safe default."""
    try:
        return max(1, int(os.environ.get("DEVMEM_MAX_CONTENT", DEFAULT_MAX_CONTENT)))
    except ValueError:
        return DEFAULT_MAX_CONTENT


def validate_content(content: str) -> None:
    """Reject content that would make a memory unsafe to retrieve in full."""
    if len(content) > max_content():
        raise ValueError(f"content exceeds the {max_content()} character limit; put long material in a file and save a short summary plus the file path")


def validate_review_after(value: str | None) -> str | None:
    """Validate an optional ISO calendar date and return its canonical form."""
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("review_after must be an ISO date (YYYY-MM-DD)") from exc


def today_utc() -> str:
    return datetime.now(UTC).date().isoformat()


def is_stale(review_after: str | None) -> bool:
    """Whether a review date has passed in UTC."""
    return bool(review_after and review_after < today_utc())


def backup_dir() -> Path:
    return Path(os.environ.get("DEVMEM_BACKUP_DIR", "~/.devmem/backups")).expanduser()


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


@contextmanager
def readonly_connection(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open an existing database without creating it or applying migrations."""
    path = path or db_path()
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
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
    if 0 < version < SCHEMA_VERSION:
        # Every upgrade of an existing database starts from a recoverable snapshot, before any step runs.
        _backup_before_upgrade(conn)
        version = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0]
    if version < 1:
        conn.executescript(V1_SCHEMA)
        conn.execute("UPDATE schema_version SET version=1")
    conn.commit()
    if version < 2:
        _migrate_v2(conn)
    if version < 3:
        _migrate_v3(conn)
    if version < 4:
        _migrate_v4(conn)
    if version < 5:
        _migrate_v5(conn)
    if version < 6:
        _migrate_v6(conn)
    if version < 7:
        _migrate_v7(conn)
    if version < 8:
        _migrate_v8(conn)
    if version < 9:
        _migrate_v9(conn)
    if version < 10:
        _migrate_v10(conn)
    if version < 11:
        _migrate_v11(conn)
    if version < 12:
        _migrate_v12(conn)


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


_V3_STATEMENTS = (
    """CREATE TABLE memory_embeddings (
      memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
      model TEXT NOT NULL, dim INTEGER NOT NULL, content_hash TEXT NOT NULL, vec BLOB NOT NULL
    )""",
    """CREATE TRIGGER memories_embeddings_ad AFTER DELETE ON memories BEGIN
      DELETE FROM memory_embeddings WHERE memory_id=old.id;
    END""",
)


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v2 -> v3.  Embeddings are deliberately independent of the foreign_keys pragma."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0] < 3:
            for statement in _V3_STATEMENTS:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version=3")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


_V4_STATEMENTS = (
    "ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
    """CREATE TABLE memory_revisions (
      id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL, project TEXT, kind TEXT NOT NULL,
      title TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, created_at TEXT,
      op TEXT NOT NULL CHECK(op IN ('update','delete')), revised_at TEXT NOT NULL
    )""",
    "CREATE INDEX memory_revisions_memory_id ON memory_revisions(memory_id, id DESC)",
    """CREATE TRIGGER memories_revisions_bu BEFORE UPDATE ON memories
      WHEN old.title IS NOT new.title OR old.content IS NOT new.content OR old.tags IS NOT new.tags
        OR old.kind IS NOT new.kind OR old.project IS NOT new.project OR old.pinned IS NOT new.pinned
      BEGIN
        INSERT INTO memory_revisions(memory_id,project,kind,title,content,tags,pinned,created_at,op,revised_at)
        VALUES(old.id,old.project,old.kind,old.title,old.content,old.tags,old.pinned,old.created_at,'update',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
      END""",
    """CREATE TRIGGER memories_revisions_bd BEFORE DELETE ON memories BEGIN
      INSERT INTO memory_revisions(memory_id,project,kind,title,content,tags,pinned,created_at,op,revised_at)
      VALUES(old.id,old.project,old.kind,old.title,old.content,old.tags,old.pinned,old.created_at,'delete',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
    END""",
)


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v3 -> v4.  History is additive and survives deleted memories."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0] < 4:
            for statement in _V4_STATEMENTS:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version=4")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


_V5_STATEMENTS = (
    """CREATE TABLE memory_links (
      id INTEGER PRIMARY KEY, from_id INTEGER NOT NULL, to_id INTEGER NOT NULL,
      type TEXT NOT NULL CHECK(type IN ('blocks','depends_on','relates_to','overlaps','fixes','supersedes')),
      note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
      UNIQUE(from_id,to_id,type), CHECK(from_id != to_id)
    )""",
    "CREATE INDEX memory_links_from_id ON memory_links(from_id)",
    "CREATE INDEX memory_links_to_id ON memory_links(to_id)",
    """CREATE TABLE memory_link_revisions (
      id INTEGER PRIMARY KEY, link_id INTEGER NOT NULL, from_id INTEGER NOT NULL, to_id INTEGER NOT NULL,
      type TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL, removed_at TEXT NOT NULL, memory_revision_id INTEGER
    )""",
    "CREATE INDEX memory_link_revisions_from_id ON memory_link_revisions(from_id)",
    "CREATE INDEX memory_link_revisions_to_id ON memory_link_revisions(to_id)",
    """CREATE TRIGGER memory_links_revisions_bd BEFORE DELETE ON memory_links BEGIN
      INSERT INTO memory_link_revisions(link_id,from_id,to_id,type,note,created_at,removed_at,memory_revision_id)
      VALUES(old.id,old.from_id,old.to_id,old.type,old.note,old.created_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'),NULL);
    END""",
    """CREATE TRIGGER memories_links_ad AFTER DELETE ON memories BEGIN
      DELETE FROM memory_links WHERE from_id=old.id OR to_id=old.id;
      UPDATE memory_link_revisions SET memory_revision_id=(SELECT MAX(id) FROM memory_revisions WHERE memory_id=old.id AND op='delete')
      WHERE memory_revision_id IS NULL AND (from_id=old.id OR to_id=old.id)
        AND removed_at=(SELECT revised_at FROM memory_revisions WHERE memory_id=old.id AND op='delete' ORDER BY id DESC LIMIT 1);
    END""",
)


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v4 -> v5.  Links and their delete history remain independent of deleted memories."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0] < 5:
            for statement in _V5_STATEMENTS:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version=5")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _backup_before_migration(conn: sqlite3.Connection, new_version: int, old_version: int) -> None:
    """Make a WAL-safe SQLite snapshot before upgrading an existing database."""
    destination_dir = backup_dir()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_dir / f"devmem-before-v{new_version}-from-v{old_version}-{stamp}.db"
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination)
        source_path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        # fix(2026-10-28): write to .tmp and rename, so a half-written file never matches the "backup exists" glob.
        partial = destination.with_name(destination.name + ".tmp")
        try:
            source, backup = sqlite3.connect(source_path), sqlite3.connect(partial)
            try: source.backup(backup)
            finally: source.close(); backup.close()
            os.replace(partial, destination)
        except BaseException:
            partial.unlink(missing_ok=True); raise
    except Exception as exc:
        raise RuntimeError(f"devmem migration backup failed ({destination}): {exc}") from exc


def _backup_before_upgrade(conn: sqlite3.Connection) -> None:
    """Snapshot an existing database once per upgrade, under the upgrade lock so racing processes do not both back up."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0]
        if 0 < version < SCHEMA_VERSION and not any(backup_dir().glob(f"devmem-before-v{SCHEMA_VERSION}-from-v{version}-*.db")):
            _backup_before_migration(conn, SCHEMA_VERSION, version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v5 -> v6.  Adds review dates; the backup is taken by migrate() before any upgrade step."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0]
        if version < 6:
            conn.execute("ALTER TABLE memories ADD COLUMN review_after TEXT")
            conn.execute("UPDATE schema_version SET version=6")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


_V7_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS maintenance_runs (
      id INTEGER PRIMARY KEY, trigger TEXT NOT NULL CHECK(trigger IN ('startup','session_end','manual')),
      project TEXT, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
      engine TEXT, model TEXT, checked INTEGER DEFAULT 0, applied INTEGER DEFAULT 0, flagged INTEGER DEFAULT 0, error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS maintenance_changes (
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, memory_id INTEGER NOT NULL, target_id INTEGER,
      action TEXT NOT NULL CHECK(action IN ('update','merge','supersede','keep','flag')),
      applied INTEGER NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL,
      old_title TEXT, old_content TEXT, new_title TEXT, new_content TEXT,
      revision_id INTEGER, link_id INTEGER, blocked_by TEXT,
      reverted_at TEXT, created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS maintenance_changes_run ON maintenance_changes(run_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS maintenance_changes_memory ON maintenance_changes(memory_id, created_at DESC)",
)


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """v6 -> v7. Maintenance history is additive; migrate() took the backup."""
    conn.commit(); conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0] < 7:
            for statement in _V7_STATEMENTS: conn.execute(statement)
            conn.execute("UPDATE schema_version SET version=7")
        conn.commit()
    except Exception:
        conn.rollback(); raise


_V8_COLUMNS = (
    # full=1 marks an unlimited, project-less, non-dry run: only those reset the startup window.
    ("maintenance_runs", "full", "INTEGER NOT NULL DEFAULT 0"),
    ("maintenance_runs", "options", "TEXT"),  # JSON for queued requests, e.g. the transcript path
    ("maintenance_runs", "note", "TEXT"),
    ("maintenance_changes", "old_review_after", "TEXT"),
    ("maintenance_changes", "new_review_after", "TEXT"),
    ("maintenance_changes", "seen_at", "TEXT"),
)


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """v7 -> v8. Additive maintenance bookkeeping; skips columns that exist (v7 tables are CREATE IF NOT EXISTS)."""
    conn.commit(); conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0] < 8:
            for table, column, declaration in _V8_COLUMNS:
                if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            conn.execute("UPDATE schema_version SET version=8")
        conn.commit()
    except Exception:
        conn.rollback(); raise


_V9_STATEMENTS = (
    # thread_id is the root message's id; a thread's status is its root's status.
    """CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY, thread_id INTEGER, reply_to INTEGER, project TEXT, sender TEXT NOT NULL,
      sender_kind TEXT NOT NULL CHECK(sender_kind IN ('claude','subagent','codex','user','system')),
      sender_declared INTEGER NOT NULL DEFAULT 0, recipient TEXT NOT NULL DEFAULT '*',
      kind TEXT NOT NULL CHECK(kind IN ('message','question','finding','event')), subject TEXT, body TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','answered','closed')),
      created_at TEXT NOT NULL, closed_at TEXT, agent_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS message_reads (
      message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, reader TEXT NOT NULL, read_at TEXT NOT NULL,
      PRIMARY KEY(message_id, reader)
    )""",
    # Claude Code subagents seen in ~/.claude/projects/<proj>/<session>/subagents; offset is this table's own read position.
    """CREATE TABLE IF NOT EXISTS subagents (
      agent_id TEXT PRIMARY KEY, session_id TEXT, project TEXT, agent_type TEXT, description TEXT, tool_use_id TEXT,
      spawn_depth INTEGER, started_at TEXT, last_activity_at TEXT, finished INTEGER NOT NULL DEFAULT 0,
      task TEXT, last_text TEXT, source_file TEXT, byte_offset INTEGER NOT NULL DEFAULT 0, meta_mtime REAL,
      spawn_msg_id INTEGER, report_msg_id INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS subagent_tools (
      id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, ts TEXT, name TEXT NOT NULL, arg TEXT NOT NULL DEFAULT '',
      source_offset INTEGER NOT NULL, UNIQUE(agent_id, source_offset)
    )""",
    "CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id, id)",
    "CREATE INDEX IF NOT EXISTS messages_sender_created ON messages(sender, created_at)",
    "CREATE INDEX IF NOT EXISTS subagents_session ON subagents(session_id, last_activity_at)",
)


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """v8 -> v9. Agent messages and live subagents; additive, migrate() took the backup."""
    conn.commit(); conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0] < 9:
            for statement in _V9_STATEMENTS: conn.execute(statement)
            conn.execute("UPDATE schema_version SET version=9")
        conn.commit()
    except Exception:
        conn.rollback(); raise


_V10_COLUMNS = (
    # last_terminal: the newest record ends the run (SubagentHandback, its result, or an end_turn text); meta "finished" is often never written.
    ("subagents", "last_terminal", "INTEGER NOT NULL DEFAULT 0"),
    ("subagents", "tail_partial", "INTEGER NOT NULL DEFAULT 0"),  # the log ended in an unterminated line at the last read
    ("subagents", "ended", "TEXT"),  # finished | stopped, once the report event is posted
)
_V10_STATEMENTS = (
    # Assistant remarks between tool calls (role agent) and later parent messages (role parent); never thinking or tool results.
    """CREATE TABLE IF NOT EXISTS subagent_texts (
      id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, ts TEXT, role TEXT NOT NULL CHECK(role IN ('agent','parent')), body TEXT NOT NULL,
      source_offset INTEGER NOT NULL, UNIQUE(agent_id, source_offset)
    )""",
    "CREATE INDEX IF NOT EXISTS messages_agent ON messages(agent_id)",
    # Re-read every subagent log once so remarks and the end-of-run marker exist for agents scanned under v9.
    "UPDATE subagents SET byte_offset=0",
)


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """v9 -> v10. Subagent remarks and end-of-run detection; additive, migrate() took the backup."""
    conn.commit(); conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0] < 10:
            for table, column, declaration in _V10_COLUMNS:
                if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            for statement in _V10_STATEMENTS: conn.execute(statement)
            conn.execute("UPDATE schema_version SET version=10")
        conn.commit()
    except Exception:
        conn.rollback(); raise


_V11_COLUMNS = (
    ("subagents", "agent_kind", "TEXT NOT NULL DEFAULT 'subagent'"),
    ("subagents", "model", "TEXT"),
    ("subagents", "handoff_id", "TEXT"),
    ("subagents", "handoff_session_id", "TEXT"),
)
_V11_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS subagents_handoff ON subagents(handoff_session_id, last_activity_at)",
    "UPDATE ingest_offsets SET byte_offset=0 WHERE source_file LIKE '%/sessions/%'",
)


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """v10 -> v11. Codex rollout identities and conservative Claude handoff links; migrate() took the backup."""
    conn.commit(); conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0] < 11:
            for table, column, declaration in _V11_COLUMNS:
                if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            for statement in _V11_STATEMENTS: conn.execute(statement)
            conn.execute("UPDATE schema_version SET version=11")
        conn.commit()
    except Exception:
        conn.rollback(); raise


def _migrate_v12(conn: sqlite3.Connection) -> None:
    """Add durable user-message dispatch jobs; migrate() took the backup."""
    conn.commit(); conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS message_jobs (
            id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
            chat_key TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', agent TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS message_jobs_queue ON message_jobs(state,id)")
        conn.execute("UPDATE schema_version SET version=12")
        conn.commit()
    except Exception:
        conn.rollback(); raise


def maintenance_failing_since(conn: sqlite3.Connection) -> dict | None:
    """{since, error} while the newest maintenance runs failed (or one is stuck running for 6 h+); None when healthy."""
    from .maintenance import config
    if not config()["enabled"]:
        return None
    try:
        rows = conn.execute("SELECT status,started_at,error FROM maintenance_runs WHERE status IN ('success','error','running') ORDER BY id DESC LIMIT 100").fetchall()
    except sqlite3.OperationalError:  # read-only connection to a pre-v7 database
        return None
    failing = None
    for row in rows:
        started = parse_ts(row["started_at"])
        if row["status"] == "success": break
        if row["status"] == "running":
            if not started or datetime.now(UTC) - started < timedelta(hours=6): continue
            row = {"started_at": row["started_at"], "error": "a run has been stuck for over 6 hours"}
        failing = {"since": row["started_at"], "error": (failing or {}).get("error") or row["error"] or "unknown error"}
    return failing


def restore_memory(conn: sqlite3.Connection, id: int, revision_id: int, source_agent: str) -> None:
    """Restore a revision using the same safeguards for MCP and maintenance reverts."""
    row = conn.execute("SELECT * FROM memory_revisions WHERE id=? AND memory_id=?", (revision_id, id)).fetchone()
    if not row:
        raise ValueError("revision does not belong to memory")
    existing = conn.execute("SELECT created_at FROM memories WHERE id=?", (id,)).fetchone()
    if existing and row["created_at"] is not None and existing["created_at"] != row["created_at"]:
        # SQLite reuses the highest id after a delete, so this id may now be another memory.
        raise ValueError("memory id was reused by a newer memory; save the revision as a new memory instead")
    if existing:
        conn.execute("UPDATE memories SET project=?,kind=?,title=?,content=?,tags=?,pinned=?,updated_at=? WHERE id=?",
                     (row["project"], row["kind"], row["title"], row["content"], row["tags"], row["pinned"], now(), id))
    else:
        conn.execute("""INSERT INTO memories(id,project,kind,title,content,tags,pinned,created_at,updated_at,source_agent)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (id, row["project"], row["kind"], row["title"], row["content"], row["tags"],
                                                row["pinned"], row["created_at"] or now(), now(), source_agent))
        if row["op"] == "delete":
            conn.execute("""INSERT OR IGNORE INTO memory_links(from_id,to_id,type,note,created_at)
                SELECT from_id,to_id,type,note,created_at FROM memory_link_revisions
                WHERE memory_revision_id=? AND (from_id=? OR to_id=?)
                  AND EXISTS(SELECT 1 FROM memories WHERE id=CASE WHEN from_id=? THEN to_id ELSE from_id END)""",
                         (revision_id, id, id, id))


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


def embedding_text(title: str, content: str, tags: str) -> str:
    return f"{title}\n{content}\n{tags}"


def embedding_hash(title: str, content: str, tags: str, model: str = EMBEDDING_MODEL) -> str:
    return hashlib.sha256((embedding_text(title, content, tags) + model).encode()).hexdigest()


def _normalise(vectors: np.ndarray) -> np.ndarray | None:
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[1] != EMBEDDING_DIM:
        return None
    lengths = np.linalg.norm(vectors, axis=1)
    if np.any(lengths == 0):
        return None
    return vectors / lengths[:, None]


def ensure_embeddings(conn: sqlite3.Connection, limit: int = 200) -> dict[str, int]:
    """Fill at most ``limit`` stale rows; embedding-model failures are silent (embedded=0); database errors propagate."""
    rows = conn.execute("""SELECT m.id,m.title,m.content,m.tags,e.model,e.content_hash,e.dim
        FROM memories m LEFT JOIN memory_embeddings e ON e.memory_id=m.id ORDER BY m.id""").fetchall()
    all_stale = [row for row in rows if row["model"] != EMBEDDING_MODEL or row["dim"] != EMBEDDING_DIM
                 or row["content_hash"] != embedding_hash(row["title"], row["content"], row["tags"])]
    stale = all_stale[:max(0, limit)]
    if not stale:
        return {"embedded": 0, "pending": 0}
    try:
        vectors = get_embedder().embed(
            [embedding_text(row["title"], row["content"], row["tags"]) for row in stale]
        )
        normalised = _normalise(vectors) if vectors is not None else None
    except Exception as exc:
        print(f"devmem: embedding normalisation failed: {type(exc).__name__}: {exc}"[:250], file=sys.stderr)
        normalised = None
    if normalised is None:
        return {"embedded": 0, "pending": len(all_stale)}
    embedded = 0
    for row, vector in zip(stale, normalised, strict=True):
        cur = conn.execute("""INSERT INTO memory_embeddings(memory_id,model,dim,content_hash,vec)
            SELECT ?,?,?,?,? WHERE EXISTS (SELECT 1 FROM memories WHERE id=?)
            ON CONFLICT(memory_id) DO UPDATE SET model=excluded.model,dim=excluded.dim,
            content_hash=excluded.content_hash,vec=excluded.vec""",
            (row["id"], EMBEDDING_MODEL, EMBEDDING_DIM,
             embedding_hash(row["title"], row["content"], row["tags"]), vector.astype("<f4").tobytes(), row["id"]))
        embedded += cur.rowcount
    # A skipped row disappeared between the read and conditional upsert, so it is not pending.
    return {"embedded": embedded, "pending": len(all_stale) - len(stale)}


def embed_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    """Embed one memory immediately; callers handle unavailable embeddings."""
    row = conn.execute("SELECT id,title,content,tags FROM memories WHERE id=?", (memory_id,)).fetchone()
    if not row:
        return False
    vectors = get_embedder().embed([embedding_text(row["title"], row["content"], row["tags"])])
    normalised = _normalise(vectors) if vectors is not None else None
    if normalised is None:
        return False
    conn.execute("""INSERT INTO memory_embeddings(memory_id,model,dim,content_hash,vec) VALUES(?,?,?,?,?)
        ON CONFLICT(memory_id) DO UPDATE SET model=excluded.model,dim=excluded.dim,
        content_hash=excluded.content_hash,vec=excluded.vec""",
        (memory_id, EMBEDDING_MODEL, EMBEDDING_DIM,
         embedding_hash(row["title"], row["content"], row["tags"]), normalised[0].astype("<f4").tobytes()))
    return True


def similar_memories(conn: sqlite3.Connection, memory_id: int, project: str | None) -> list[dict]:
    """Return nearby embedded memories in the same scope."""
    row = conn.execute("SELECT vec FROM memory_embeddings WHERE memory_id=? AND model=? AND dim=?",
                       (memory_id, EMBEDDING_MODEL, EMBEDDING_DIM)).fetchone()
    if not row:
        return []
    where, params = ("m.project IS NULL", ()) if project is None else ("m.project=?", (project,))
    rows = conn.execute(f"""SELECT m.id,m.title,e.vec FROM memories m JOIN memory_embeddings e ON e.memory_id=m.id
        WHERE {where} AND m.id!=? AND e.model=? AND e.dim=?""",
        (*params, memory_id, EMBEDDING_MODEL, EMBEDDING_DIM)).fetchall()
    vector = np.frombuffer(row["vec"], dtype="<f4")
    similar = []
    for candidate in rows:
        other = np.frombuffer(candidate["vec"], dtype="<f4")
        if vector.size != EMBEDDING_DIM or other.size != EMBEDDING_DIM:
            continue
        score = float(np.dot(vector, other))
        if score >= SIMILAR_COSINE:
            similar.append({"id": candidate["id"], "title": candidate["title"], "score": round(score, 3)})
    return sorted(similar, key=lambda item: (-item["score"], item["id"]))[:3]


def not_superseded_sql(alias: str = "m") -> str:
    return f"NOT EXISTS(SELECT 1 FROM memory_links sl WHERE sl.to_id={alias}.id AND sl.type='supersedes' AND sl.note='automatic maintenance')"


def _bm25_candidates(conn: sqlite3.Connection, query: str, where_sql: str, params: tuple, kind: str | None) -> list[dict]:
    match = fts_query(query)
    if not match:
        return []
    clauses, values = [f"({where_sql})", "memories_fts MATCH ?", not_superseded_sql()], list(params) + [match]
    if kind:
        clauses.append("m.kind=?")
        values.append(kind)
    rows = conn.execute(f"""SELECT m.id,m.title,m.kind,m.project,m.updated_at,m.review_after,
        snippet(memories_fts,1,'<b>','</b>','…',18) snippet
        FROM memories_fts JOIN memories m ON m.id=memories_fts.rowid
        WHERE {' AND '.join(clauses)} ORDER BY bm25(memories_fts) LIMIT 50""", values).fetchall()
    return [{**dict(row), "stale": is_stale(row["review_after"])} for row in rows]


def hybrid_search(conn: sqlite3.Connection, query: str, *, where_sql: str, params: tuple = (),
                  kind: str | None = None, limit: int = 8) -> list[dict]:
    """Fuse FTS and local-vector rankings using Reciprocal Rank Fusion (k=60)."""
    keyword = _bm25_candidates(conn, query, where_sql, params, kind)
    for row in keyword:
        row["match"] = "keyword"  # same result shape on the BM25-only fallback paths
    if not fts_query(query):
        return keyword[:limit]
    if os.environ.get("DEVMEM_EMBED") == "0":
        return keyword[:limit]

    # These may populate stale rows and load the local model.  Both are best-effort.
    try:
        ensure_embeddings(conn)
    except sqlite3.Error as exc:
        print(f"devmem: embedding backfill skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
    try:
        query_vector = get_embedder().embed([query], query=True)
        normalised_query = _normalise(query_vector) if query_vector is not None else None
    except Exception as exc:
        print(f"devmem: query embedding normalisation failed: {type(exc).__name__}: {exc}"[:250], file=sys.stderr)
        normalised_query = None
    if normalised_query is None:
        return keyword[:limit]

    clauses, values = [f"({where_sql})", "e.model=?", "e.dim=?", not_superseded_sql()], list(params) + [EMBEDDING_MODEL, EMBEDDING_DIM]
    if kind:
        clauses.append("m.kind=?")
        values.append(kind)
    rows = conn.execute(f"""SELECT m.id,m.title,m.kind,m.project,m.updated_at,m.review_after,m.content,e.vec
        FROM memories m JOIN memory_embeddings e ON e.memory_id=m.id
        WHERE {' AND '.join(clauses)}""", values).fetchall()
    semantic: list[dict] = []
    q = normalised_query[0]
    for row in rows:
        vector = np.frombuffer(row["vec"], dtype="<f4")
        if vector.size != EMBEDDING_DIM:
            continue
        cosine = float(np.dot(q, vector))
        if cosine >= MIN_COSINE:
            item = dict(row)
            item["snippet"] = item.pop("content")[:160]
            item.pop("vec")
            item["cosine"] = cosine
            semantic.append(item)
    semantic.sort(key=lambda row: (-row["cosine"], row["id"]))
    semantic = semantic[:50]

    merged: dict[int, dict] = {}
    for rank, row in enumerate(keyword, 1):
        merged[row["id"]] = {**row, "_score": 1 / (60 + rank), "_keyword": True, "_semantic": False}
    for rank, row in enumerate(semantic, 1):
        current = merged.get(row["id"])
        if current is None:
            merged[row["id"]] = {**row, "_score": 1 / (60 + rank), "_keyword": False, "_semantic": True}
        else:
            current["_score"] += 1 / (60 + rank)
            current["_semantic"] = True
    ordered = sorted(merged.values(), key=lambda row: (-row["_score"], row["id"]))[:limit]
    out = []
    for row in ordered:
        keyword_match, semantic_match = row.pop("_keyword"), row.pop("_semantic")
        row.pop("_score")
        row.pop("cosine", None)
        row["match"] = "both" if keyword_match and semantic_match else "keyword" if keyword_match else "semantic"
        row["stale"] = is_stale(row.get("review_after"))
        out.append(row)
    return out


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


def context_text(conn: sqlite3.Connection, project: str | None) -> str:
    """Render the compact project and global-memory briefing."""
    current_date = today_utc()
    visible = not_superseded_sql("memories")
    pinned_rows = conn.execute(f"SELECT id,kind,title,content,project FROM memories WHERE pinned=1 AND ({visible}) AND (review_after IS NULL OR review_after>=?) ORDER BY updated_at DESC", (current_date,)).fetchall()
    pinned_text, cap, last_id = "", 6000 - len("## Pinned\n"), None
    for row in pinned_rows:
        name = (Path(row["project"]).name or row["project"]) if row["project"] else "global"
        item = f"[{row['kind']} #{row['id']} · {name}] {row['title']}\n{row['content']}"
        note = f"… (truncated; memory_get {row['id']} for the rest)"
        separator = "\n" if pinned_text else ""
        if len(pinned_text) + len(separator) + len(item) <= cap:
            pinned_text += separator + item
            last_id = row["id"]
            continue
        if len(pinned_text) + len(separator) + len(note) > cap:
            note = f"… (truncated; memory_get {last_id} for the rest)"
            pinned_text = pinned_text[:cap - len(note) - 1]
            separator = "\n"
        available = cap - len(pinned_text) - len(separator) - len(note) - 1
        if available > 0:
            pinned_text += separator + item[:available] + "\n"
        elif pinned_text:
            pinned_text += "\n"
        pinned_text += note
        break
    rows = conn.execute(f"""SELECT kind,title,content FROM memories WHERE pinned=0 AND project=? AND kind IN ('decision','preference','todo','note') AND ({visible})
        AND (review_after IS NULL OR review_after>=?) ORDER BY kind='note', updated_at DESC LIMIT 20""", (project, current_date)).fetchall()
    global_rows = conn.execute(f"""SELECT kind,title,content FROM memories WHERE pinned=0 AND project IS NULL AND kind IN ('decision','preference') AND ({visible})
        AND (review_after IS NULL OR review_after>=?) ORDER BY updated_at DESC LIMIT 40""", (current_date,)).fetchall()
    text = "\n".join(f"[{r['kind']}] {r['title']}: {r['content'][:200]}" for r in rows)[:1500]
    global_text = "\n".join(f"[{r['kind']}] {r['title']}" for r in global_rows)[:3000]  # titles state the rule; memory_get has the detail
    failing = maintenance_failing_since(conn)
    failing_text = f"Automatic maintenance failing since {failing['since'][:16]}: {failing['error'][:160]} (see the panel's Maintenance page)" if failing else ""
    parts = [f"## Pinned\n{pinned_text}" if pinned_text else "", f"## Project\n{text}" if text else "", f"## Global preferences\n{global_text}" if global_text else "", f"## Maintenance\n{failing_text}" if failing_text else ""]
    return "\n\n".join(p for p in parts if p) or "No saved project context."


def _outdated_terms() -> list[str]:
    """Read optional audit wording without making it a required configuration file."""
    path = Path(os.environ.get("DEVMEM_AUDIT", "~/.devmem/audit.json")).expanduser()
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        terms = data.get("outdated_terms", []) if isinstance(data, dict) else []
        return [term.casefold() for term in terms if isinstance(term, str) and term.strip()]
    except FileNotFoundError:
        return []
    except Exception:
        return []


def audit_memories(conn: sqlite3.Connection) -> list[dict]:
    """Return report-only context-rot findings, sorted by category then largest memory."""
    cap, today = max_content(), today_utc()
    terms = _outdated_terms()
    rows = [dict(row) for row in conn.execute(f"""SELECT id,title,content,project,updated_at,review_after FROM memories
        WHERE {not_superseded_sql('memories')}""").fetchall()]
    findings = []
    for row in rows:
        base = {key: row[key] for key in ("id", "title", "updated_at")}
        base["size"] = len(row["content"])
        if base["size"] > cap:
            findings.append({**base, "reason": f"oversized (over {cap} characters)"})
        if row["review_after"] and row["review_after"] < today:
            findings.append({**base, "reason": "review date has passed"})
        parsed = parse_ts(row["updated_at"])
        if parsed and (datetime.now(UTC) - parsed).days >= 30:
            findings.append({**base, "reason": "not updated in 30+ days"})
        haystack = (row["title"] + "\n" + row["content"]).casefold()
        matched = [term for term in terms if term in haystack]
        if matched:
            findings.append({**base, "reason": "outdated wording: " + ", ".join(matched)})

    embedded = conn.execute(f"""SELECT m.id,m.title,m.content,m.project,m.updated_at,e.vec
        FROM memories m JOIN memory_embeddings e ON e.memory_id=m.id WHERE e.model=? AND e.dim=?
        AND {not_superseded_sql()} ORDER BY m.id""",
                            (EMBEDDING_MODEL, EMBEDDING_DIM)).fetchall()
    for index, left in enumerate(embedded):
        left_vector = np.frombuffer(left["vec"], dtype="<f4")
        if left_vector.size != EMBEDDING_DIM:
            continue
        for right in embedded[index + 1:]:
            if left["project"] != right["project"]:
                continue
            right_vector = np.frombuffer(right["vec"], dtype="<f4")
            if right_vector.size != EMBEDDING_DIM:
                continue
            score = float(np.dot(left_vector, right_vector))
            if score >= 0.86:
                findings.append({"id": left["id"], "title": left["title"], "size": len(left["content"]),
                                 "updated_at": left["updated_at"], "reason": f"likely duplicate of #{right['id']} ({score:.2f})",
                                 "other_id": right["id"], "other_title": right["title"], "score": round(score, 3)})
    return sorted(findings, key=lambda item: (item["reason"], -item["size"], item["id"]))
