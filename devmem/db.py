"""SQLite storage for devmem.  All dates are UTC ISO-8601 strings."""
from __future__ import annotations

import os
import re
import sqlite3
import hashlib
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import numpy as np

from .embed import EMBEDDING_DIM, EMBEDDING_MODEL, get_embedder

SCHEMA_VERSION = 3
# bge-small-en-v1.5 scores unrelated text ~0.45-0.59 and real matches ~0.65+ (measured on ~40 memories, 2026-10).
MIN_COSINE = 0.6


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
    if version < 1:
        conn.executescript(V1_SCHEMA)
        conn.execute("UPDATE schema_version SET version=1")
    conn.commit()
    if version < 2:
        _migrate_v2(conn)
    if version < 3:
        _migrate_v3(conn)


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


def _bm25_candidates(conn: sqlite3.Connection, query: str, where_sql: str, params: tuple, kind: str | None) -> list[dict]:
    match = fts_query(query)
    if not match:
        return []
    clauses, values = [f"({where_sql})", "memories_fts MATCH ?"], list(params) + [match]
    if kind:
        clauses.append("m.kind=?")
        values.append(kind)
    rows = conn.execute(f"""SELECT m.id,m.title,m.kind,m.project,m.updated_at,
        snippet(memories_fts,1,'<b>','</b>','…',18) snippet
        FROM memories_fts JOIN memories m ON m.id=memories_fts.rowid
        WHERE {' AND '.join(clauses)} ORDER BY bm25(memories_fts) LIMIT 50""", values).fetchall()
    return [dict(row) for row in rows]


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

    clauses, values = [f"({where_sql})", "e.model=?", "e.dim=?"], list(params) + [EMBEDDING_MODEL, EMBEDDING_DIM]
    if kind:
        clauses.append("m.kind=?")
        values.append(kind)
    rows = conn.execute(f"""SELECT m.id,m.title,m.kind,m.project,m.updated_at,m.content,e.vec
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
    rows = conn.execute("""SELECT kind,title,content FROM memories WHERE project=? AND kind IN ('decision','preference','todo','note')
        ORDER BY kind='note', updated_at DESC LIMIT 20""", (project,)).fetchall()
    global_rows = conn.execute("SELECT kind,title,content FROM memories WHERE project IS NULL AND kind IN ('decision','preference') ORDER BY updated_at DESC LIMIT 40").fetchall()
    text = "\n".join(f"[{r['kind']}] {r['title']}: {r['content'][:200]}" for r in rows)[:1500]
    global_text = "\n".join(f"[{r['kind']}] {r['title']}" for r in global_rows)[:3000]  # titles state the rule; memory_get has the detail
    parts = [f"## Project\n{text}" if text else "", f"## Global preferences\n{global_text}" if global_text else ""]
    return "\n\n".join(p for p in parts if p) or "No saved project context."
