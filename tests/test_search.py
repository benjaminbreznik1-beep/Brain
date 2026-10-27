"""Offline hybrid-search coverage: no test here instantiates FastEmbed."""
from __future__ import annotations

import numpy as np

from devmem.db import connect, embedding_hash, ensure_embeddings, hybrid_search
from devmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, set_embedder


def _vector(*values: float) -> np.ndarray:
    result = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    result[:len(values)] = values
    return result / np.linalg.norm(result)


class FakeEmbedder:
    def __init__(self, documents=None, queries=None, raises=False):
        self.documents = documents or {}
        self.queries = queries or {}
        self.raises = raises
        self.calls = []

    def embed(self, texts, *, query=False):
        self.calls.append((list(texts), query))
        if self.raises:
            raise RuntimeError("offline fake failure")
        source = self.queries if query else self.documents
        return np.asarray([source[text] for text in texts], dtype=np.float32)


def _memory(conn, title, content, *, project="/project", kind="note", tags="") -> int:
    return conn.execute("""INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at)
        VALUES(?,?,?,?,?,'now','now')""", (project, kind, title, content, tags)).lastrowid


def _embedding(conn, memory_id, title, content, tags, vector):
    conn.execute("""INSERT INTO memory_embeddings(memory_id,model,dim,content_hash,vec)
        VALUES(?,?,?,?,?)""", (memory_id, EMBEDDING_MODEL, EMBEDDING_DIM,
        embedding_hash(title, content, tags), _vector(*vector).astype("<f4").tobytes()))


def test_rrf_promotes_a_memory_present_in_both_rankings(tmp_path):
    conn = connect(tmp_path / "memory.db")
    keyword_only = _memory(conn, "keyword", "alpha")
    both = _memory(conn, "both", "alpha")
    semantic_only = _memory(conn, "semantic", "unrelated")
    _embedding(conn, keyword_only, "keyword", "alpha", "", (-1, 0))
    _embedding(conn, both, "both", "alpha", "", (1, 0))
    _embedding(conn, semantic_only, "semantic", "unrelated", "", (.9, .44))
    set_embedder(FakeEmbedder(queries={"alpha": _vector(1, 0)}))

    rows = hybrid_search(conn, "alpha", where_sql="m.project=?", params=("/project",), limit=8)

    assert rows[0]["id"] == both
    assert {row["id"]: row["match"] for row in rows} == {
        keyword_only: "keyword", both: "both", semantic_only: "semantic"
    }
    set_embedder(None)


def test_semantic_only_hit_and_scope_kind_filters(tmp_path):
    conn = connect(tmp_path / "memory.db")
    both = _memory(conn, "latency", "latency", project="/a", kind="decision")
    semantic = _memory(conn, "speed", "throughput", project="/a", kind="decision")
    wrong_project = _memory(conn, "other", "throughput", project="/b", kind="decision")
    wrong_kind = _memory(conn, "note", "throughput", project="/a", kind="note")
    for memory_id, title, content in ((both, "latency", "latency"), (semantic, "speed", "throughput"),
                                      (wrong_project, "other", "throughput"), (wrong_kind, "note", "throughput")):
        _embedding(conn, memory_id, title, content, "", (1, 0))
    set_embedder(FakeEmbedder(queries={"latency": _vector(1, 0)}))

    rows = hybrid_search(conn, "latency", where_sql="m.project=?", params=("/a",), kind="decision", limit=8)

    assert [row["id"] for row in rows] == [both, semantic]
    assert [row["match"] for row in rows] == ["both", "semantic"]
    set_embedder(None)


def test_disabled_or_failed_embeddings_are_plain_bm25(tmp_path, monkeypatch):
    conn = connect(tmp_path / "memory.db")
    _memory(conn, "alpha", "alpha")
    _memory(conn, "other", "alpha")
    monkeypatch.setenv("DEVMEM_EMBED", "0")
    set_embedder(FakeEmbedder(raises=True))
    expected = hybrid_search(conn, "alpha", where_sql="m.project=?", params=("/project",), limit=8)
    assert hybrid_search(conn, "alpha", where_sql="m.project=?", params=("/project",), limit=8) == expected
    monkeypatch.delenv("DEVMEM_EMBED")
    set_embedder(FakeEmbedder(raises=True))
    assert hybrid_search(conn, "alpha", where_sql="m.project=?", params=("/project",), limit=8) == expected
    set_embedder(None)


def test_ensure_refreshes_and_delete_trigger_removes_embedding(tmp_path):
    conn = connect(tmp_path / "memory.db")
    memory_id = _memory(conn, "first", "one")
    fake = FakeEmbedder(documents={"first\none\n": _vector(1, 0), "first\ntwo\n": _vector(0, 1)})
    set_embedder(fake)

    assert ensure_embeddings(conn) == {"embedded": 1, "pending": 0}
    old_hash = conn.execute("SELECT content_hash FROM memory_embeddings WHERE memory_id=?", (memory_id,)).fetchone()[0]
    conn.execute("UPDATE memories SET content='two' WHERE id=?", (memory_id,))
    assert ensure_embeddings(conn) == {"embedded": 1, "pending": 0}
    assert conn.execute("SELECT content_hash FROM memory_embeddings WHERE memory_id=?", (memory_id,)).fetchone()[0] != old_hash
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    assert conn.execute("SELECT * FROM memory_embeddings WHERE memory_id=?", (memory_id,)).fetchone() is None
    set_embedder(None)


def test_v2_database_migrates_to_memory_embeddings(tmp_path):
    path = tmp_path / "previous.db"
    conn = connect(path)
    conn.execute("DROP TRIGGER memories_embeddings_ad")
    conn.execute("DROP TABLE memory_embeddings")
    conn.execute("UPDATE schema_version SET version=2")
    conn.commit()
    conn.close()

    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    assert migrated.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_embeddings'").fetchone()
    assert migrated.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='memories_embeddings_ad'").fetchone()
