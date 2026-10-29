"""Memory history, duplicate hints, and pinned-briefing coverage."""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from devmem import mcp_server
from devmem.db import (EMBEDDING_DIM, EMBEDDING_MODEL, V1_SCHEMA, _migrate_v2, _migrate_v3, _migrate_v4,
                       connect, connection, context_text, embedding_hash)
from devmem.embed import set_embedder


def _vector(*values: float) -> np.ndarray:
    result = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    result[:len(values)] = values
    return result / np.linalg.norm(result)


class FakeEmbedder:
    def __init__(self, documents=None, raises=False):
        self.documents = documents or {}
        self.raises = raises

    def embed(self, texts, *, query=False):
        if self.raises:
            raise RuntimeError("offline fake failure")
        return np.asarray([self.documents[text] for text in texts], dtype=np.float32)


@pytest.fixture
def memory_tools(monkeypatch, tmp_path):
    project = str(tmp_path / "project")
    monkeypatch.setattr(mcp_server, "_call", lambda tool, args, fn, ctx=None: fn())
    monkeypatch.setattr(mcp_server, "_client", lambda ctx=None: (1, project))
    return project


def test_update_delete_and_history_keep_prior_versions(memory_tools):
    saved = mcp_server.memory_save("old", title="Old")
    memory_id = saved["id"]
    assert mcp_server.memory_get(memory_id)["revisions"] == 0
    mcp_server.memory_update(memory_id, content="new")
    mcp_server.memory_update(memory_id, content="new")
    assert [(row["op"], row["content"]) for row in mcp_server.memory_history(memory_id)] == [("update", "old")]
    assert mcp_server.memory_get(memory_id)["revisions"] == 1
    mcp_server.memory_delete(memory_id)
    history = mcp_server.memory_history(memory_id, limit=1)
    assert [(row["op"], row["content"]) for row in history] == [("delete", "new")]
    assert mcp_server.memory_history(memory_id, limit=99)[-1]["content"] == "old"


def test_restore_existing_deleted_and_wrong_revision(memory_tools):
    first = mcp_server.memory_save("one", title="One")["id"]
    second = mcp_server.memory_save("two", title="Two")["id"]
    mcp_server.memory_update(first, content="changed")
    revision = mcp_server.memory_history(first)[0]["id"]
    assert mcp_server.memory_restore(first, revision)["content"] == "one"
    assert mcp_server.memory_history(first)[0]["content"] == "changed"
    mcp_server.memory_delete(first)
    deleted = mcp_server.memory_history(first)[0]["id"]
    assert mcp_server.memory_restore(first, deleted)["id"] == first
    mcp_server.memory_update(second, content="other")
    with pytest.raises(ValueError):
        mcp_server.memory_restore(first, mcp_server.memory_history(second)[0]["id"])


def test_restore_keeps_created_at_and_refuses_a_reused_id(memory_tools):
    newest = mcp_server.memory_save("original", title="Original")["id"]
    created = mcp_server.memory_get(newest)["created_at"]
    mcp_server.memory_delete(newest)
    deleted = mcp_server.memory_history(newest)[0]["id"]
    assert mcp_server.memory_restore(newest, deleted)["created_at"] == created
    mcp_server.memory_delete(newest)
    reused = mcp_server.memory_save("unrelated", title="Unrelated")["id"]
    assert reused == newest  # SQLite hands the deleted highest id to the next row
    with pytest.raises(ValueError, match="reused"):
        mcp_server.memory_restore(newest, deleted)
    assert mcp_server.memory_get(newest)["content"] == "unrelated"


def test_v3_migrates_to_v4_without_losing_rows_or_embeddings(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(V1_SCHEMA)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (1)")
    _migrate_v2(conn); _migrate_v3(conn)
    memory_id = conn.execute("""INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at)
        VALUES(NULL,'note','title','body','tag','x','x')""").lastrowid
    vector = _vector(1, 0).astype("<f4").tobytes()
    conn.execute("INSERT INTO memory_embeddings VALUES(?,?,?,?,?)", (memory_id, EMBEDDING_MODEL, EMBEDDING_DIM, embedding_hash("title", "body", "tag"), vector))
    conn.commit(); conn.close()
    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 12
    assert migrated.execute("SELECT project,pinned FROM memories").fetchone()[:] == (None, 0)
    assert migrated.execute("SELECT vec FROM memory_embeddings WHERE memory_id=?", (memory_id,)).fetchone()[0] == vector
    migrated.close()
    again = connect(path)
    assert again.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_v4_migrates_to_v5_without_losing_rows_revisions_or_embeddings(tmp_path):
    path = tmp_path / "v4.db"
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    conn.executescript(V1_SCHEMA)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)"); conn.execute("INSERT INTO schema_version VALUES (1)")
    _migrate_v2(conn); _migrate_v3(conn); _migrate_v4(conn)
    memory_id = conn.execute("INSERT INTO memories(project,kind,title,content,tags,pinned,created_at,updated_at) VALUES(NULL,'note','title','body','tag',0,'x','x')").lastrowid
    conn.execute("UPDATE memories SET content='new' WHERE id=?", (memory_id,))
    vector = _vector(1, 0).astype("<f4").tobytes()
    conn.execute("INSERT INTO memory_embeddings VALUES(?,?,?,?,?)", (memory_id, EMBEDDING_MODEL, EMBEDDING_DIM, embedding_hash("title", "new", "tag"), vector))
    conn.commit(); conn.close()
    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 12
    assert migrated.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert migrated.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 1
    assert migrated.execute("SELECT vec FROM memory_embeddings WHERE memory_id=?", (memory_id,)).fetchone()[0] == vector
    migrated.close()
    again = connect(path)
    assert again.execute("SELECT version FROM schema_version").fetchone()[0] == 12


def test_memory_links_validate_list_unlink_and_restore(memory_tools):
    first = mcp_server.memory_save("one", title="One")["id"]
    second = mcp_server.memory_save("two", title="Two")["id"]
    third = mcp_server.memory_save("three", title="Three")["id"]
    with pytest.raises(ValueError): mcp_server.memory_link(first, 999, "blocks")
    with pytest.raises(ValueError): mcp_server.memory_link(first, second, "bad")
    with pytest.raises(ValueError): mcp_server.memory_link(first, first, "blocks")
    link = mcp_server.memory_link(first, second, "blocks", "old")
    assert mcp_server.memory_link(first, second, "blocks", "new")["id"] == link["id"]
    mcp_server.memory_link(second, first, "relates_to")
    assert [(l["type"], l["direction"], l["other_id"], l["note"]) for l in mcp_server.memory_get(first)["links"]] == [("blocks", "out", second, "new"), ("relates_to", "in", second, "")]
    assert mcp_server.memory_unlink(first, second, "blocks") == {"removed": 1}
    assert mcp_server.memory_unlink(second, first) == {"removed": 1}
    mcp_server.memory_link(first, second, "depends_on")
    mcp_server.memory_link(first, third, "fixes")
    mcp_server.memory_delete(first)
    revision = mcp_server.memory_history(first)[0]["id"]
    with connection() as conn:
        archived = conn.execute("SELECT memory_revision_id FROM memory_link_revisions ORDER BY id DESC LIMIT 2").fetchall()
    assert {r["memory_revision_id"] for r in archived} == {revision}
    mcp_server.memory_delete(third)
    assert mcp_server.memory_restore(first, revision)["id"] == first
    assert [(l["type"], l["other_id"]) for l in mcp_server.memory_get(first)["links"]] == [("depends_on", second)]


def test_similar_hints_are_scoped_and_embedding_failures_do_not_block(memory_tools, monkeypatch):
    vectors = {
        "First\nmatching fact\n": _vector(1, 0),
        "Second\nmatching fact again\n": _vector(1, 0),
        "Other\nunrelated\n": _vector(0, 1),
        "Global\nmatching fact again\n": _vector(1, 0),
    }
    monkeypatch.delenv("DEVMEM_EMBED", raising=False)
    set_embedder(FakeEmbedder(documents=vectors))
    try:
        first = mcp_server.memory_save("matching fact", title="First")["id"]
        assert mcp_server.memory_save("matching fact again", title="Second")["similar"][0]["id"] == first
        assert "similar" not in mcp_server.memory_save("unrelated", title="Other")
        assert "similar" not in mcp_server.memory_save("matching fact again", title="Global", scope="global")
        monkeypatch.setenv("DEVMEM_EMBED", "0")
        assert set(mcp_server.memory_save("off", title="Off")) == {"id"}
        monkeypatch.delenv("DEVMEM_EMBED", raising=False)
        set_embedder(FakeEmbedder(raises=True))
        assert set(mcp_server.memory_save("broken", title="Broken")) == {"id"}
    finally:
        set_embedder(None)


def test_pinned_context_is_first_not_duplicated_capped_and_old_format_without_pins(tmp_path):
    conn = connect(tmp_path / "memory.db")
    conn.execute("""INSERT INTO memories(project,kind,title,content,tags,pinned,created_at,updated_at)
        VALUES('/this','decision','Project','project body','',0,'x','x')""")
    conn.execute("""INSERT INTO memories(project,kind,title,content,tags,pinned,created_at,updated_at)
        VALUES('/other','note','Pinned','pinned body','',1,'x','z')""")
    text = context_text(conn, "/this")
    assert text.startswith("## Pinned\n[note #2 · other] Pinned\npinned body")
    assert text.count("Pinned") == 2 and "## Project\n[decision] Project: project body" in text
    conn.execute("UPDATE memories SET content=? WHERE id=2", ("x" * 7000,))
    capped = context_text(conn, "/this")
    assert len(capped.split("\n\n", 1)[0]) <= 6000 and "truncated; memory_get 2" in capped
    conn.execute("UPDATE memories SET pinned=0 WHERE id=2")
    assert context_text(conn, "/this") == "## Project\n[decision] Project: project body"
