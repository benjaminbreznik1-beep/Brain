"""Expiry, size limits, migration snapshots, and report-only audits."""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from devmem import db, mcp_server
from devmem.db import (EMBEDDING_DIM, EMBEDDING_MODEL, V1_SCHEMA, _migrate_v2, _migrate_v3,
                       _migrate_v4, _migrate_v5, audit_memories, connect, context_text)


def _v5_database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(V1_SCHEMA)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (1)")
    _migrate_v2(conn); _migrate_v3(conn); _migrate_v4(conn); _migrate_v5(conn)
    conn.execute("INSERT INTO memories(project,kind,title,content,tags,pinned,created_at,updated_at) VALUES(NULL,'note','old','body','',0,'x','x')")
    conn.commit(); conn.close()


def test_v5_to_v6_makes_snapshot_before_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "v5.db"; backups = tmp_path / "backups"
    monkeypatch.setenv("DEVMEM_BACKUP_DIR", str(backups))
    _v5_database(path)
    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 12
    assert migrated.execute("SELECT review_after FROM memories").fetchone()[0] is None
    backup = next(backups.glob("devmem-before-v12-from-v5-*.db"))
    previous = sqlite3.connect(backup)
    assert previous.execute("SELECT version FROM schema_version").fetchone()[0] == 5


def test_migration_skips_backup_for_new_or_current_database(tmp_path, monkeypatch):
    backups = tmp_path / "backups"; monkeypatch.setenv("DEVMEM_BACKUP_DIR", str(backups))
    path = tmp_path / "new.db"
    connect(path).close()
    assert not backups.exists()
    connect(path).close()
    assert not backups.exists()


def test_failed_backup_aborts_migration(tmp_path, monkeypatch):
    path = tmp_path / "v5.db"; _v5_database(path)
    monkeypatch.setattr(db, "_backup_before_migration", lambda *_: (_ for _ in ()).throw(RuntimeError("backup unavailable")))
    with pytest.raises(RuntimeError, match="backup unavailable"):
        connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    assert "review_after" not in {row[1] for row in raw.execute("PRAGMA table_info(memories)")}


@pytest.fixture
def memory_tools(monkeypatch, tmp_path):
    project = tmp_path / "project"
    monkeypatch.setattr(mcp_server, "_call", lambda tool, args, fn, ctx=None: fn())
    monkeypatch.setattr(mcp_server, "_client", lambda ctx=None: (1, str(project)))
    return project


def test_content_cap_and_stale_search_list_and_briefing(memory_tools, monkeypatch):
    monkeypatch.setenv("DEVMEM_MAX_CONTENT", "4")
    with pytest.raises(ValueError, match="put long material in a file"):
        mcp_server.memory_save("12345")
    fresh = mcp_server.memory_save("good", title="Fresh", review_after=(date.today() + timedelta(days=1)).isoformat())["id"]
    stale = mcp_server.memory_save("old", title="Stale", review_after=(date.today() - timedelta(days=1)).isoformat())["id"]
    with pytest.raises(ValueError, match="put long material in a file"):
        mcp_server.memory_update(fresh, content="12345")
    assert {row["id"]: row["stale"] for row in mcp_server.memory_list(scope="all")}[stale] is True
    assert mcp_server.memory_search("old", scope="all")[0]["stale"] is True
    with db.connection() as conn:
        briefing = context_text(conn, str(memory_tools))
    assert "Stale" not in briefing


def test_audit_reports_every_category(tmp_path, monkeypatch):
    path = tmp_path / "audit.db"; audit_file = tmp_path / "audit.json"
    audit_file.write_text('{"outdated_terms": ["obsolete-word"]}', encoding="utf-8")
    monkeypatch.setenv("DEVMEM_AUDIT", str(audit_file)); monkeypatch.setenv("DEVMEM_MAX_CONTENT", "10")
    conn = connect(path)
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    first = conn.execute("INSERT INTO memories(project,kind,title,content,tags,pinned,review_after,created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,?)",
                         ("/p", "note", "obsolete-word", "x" * 11, "", 0, (date.today() - timedelta(days=1)).isoformat(), old, old)).lastrowid
    second = conn.execute("INSERT INTO memories(project,kind,title,content,tags,pinned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                          ("/p", "note", "pair", "small", "", 0, old, old)).lastrowid
    vector = np.zeros(EMBEDDING_DIM, dtype=np.float32); vector[0] = 1
    for memory_id in (first, second):
        conn.execute("INSERT INTO memory_embeddings VALUES(?,?,?,?,?)", (memory_id, EMBEDDING_MODEL, EMBEDDING_DIM, "test", vector.astype("<f4").tobytes()))
    reasons = [row["reason"] for row in audit_memories(conn)]
    assert any("oversized" in reason for reason in reasons)
    assert any("review date" in reason for reason in reasons)
    assert any("likely duplicate" in reason for reason in reasons)
    assert any("30+ days" in reason for reason in reasons)
    assert any("outdated wording" in reason for reason in reasons)


def test_older_database_is_backed_up_before_the_first_upgrade_step(tmp_path, monkeypatch):
    from devmem.db import V1_SCHEMA, _migrate_v2, _migrate_v3
    path = tmp_path / "v3.db"; backups = tmp_path / "backups"
    monkeypatch.setenv("DEVMEM_BACKUP_DIR", str(backups))
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)"); raw.execute("INSERT INTO schema_version VALUES (1)")
    raw.executescript(V1_SCHEMA); raw.commit()
    _migrate_v2(raw); _migrate_v3(raw)
    raw.execute("INSERT INTO memories(project,kind,title,content,created_at,updated_at) VALUES('p','note','Old','body','x','x')"); raw.commit(); raw.close()
    connect(path).close()
    backup = sqlite3.connect(next(backups.glob("devmem-before-v12-from-v3-*.db")))
    assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    assert backup.execute("SELECT title FROM memories").fetchone()[0] == "Old"
    connect(path).close()
    assert len(list(backups.glob("*.db"))) == 1  # already current: no second backup
