"""Offline hybrid-search coverage: no test here instantiates FastEmbed."""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

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


@pytest.fixture
def fake_embedder(monkeypatch):
    """Install a fake explicitly and always put process-global state back."""
    def install(fake):
        monkeypatch.delenv("DEVMEM_EMBED", raising=False)
        set_embedder(fake)
        return fake
    try:
        yield install
    finally:
        set_embedder(None)


def _memory(conn, title, content, *, project="/project", kind="note", tags="") -> int:
    return conn.execute("""INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at)
        VALUES(?,?,?,?,?,'now','now')""", (project, kind, title, content, tags)).lastrowid


def _embedding(conn, memory_id, title, content, tags, vector):
    conn.execute("""INSERT INTO memory_embeddings(memory_id,model,dim,content_hash,vec)
        VALUES(?,?,?,?,?)""", (memory_id, EMBEDDING_MODEL, EMBEDDING_DIM,
        embedding_hash(title, content, tags), _vector(*vector).astype("<f4").tobytes()))


def test_rrf_promotes_a_memory_present_in_both_rankings(tmp_path, fake_embedder):
    conn = connect(tmp_path / "memory.db")
    keyword_only = _memory(conn, "keyword", "alpha")
    both = _memory(conn, "both", "alpha")
    semantic_only = _memory(conn, "semantic", "unrelated")
    _embedding(conn, keyword_only, "keyword", "alpha", "", (-1, 0))
    _embedding(conn, both, "both", "alpha", "", (1, 0))
    _embedding(conn, semantic_only, "semantic", "unrelated", "", (.9, .44))
    fake_embedder(FakeEmbedder(queries={"alpha": _vector(1, 0)}))

    rows = hybrid_search(conn, "alpha", where_sql="m.project=?", params=("/project",), limit=8)

    assert rows[0]["id"] == both
    assert {row["id"]: row["match"] for row in rows} == {
        keyword_only: "keyword", both: "both", semantic_only: "semantic"
    }


def test_semantic_only_hit_and_scope_kind_filters(tmp_path, fake_embedder):
    conn = connect(tmp_path / "memory.db")
    both = _memory(conn, "latency", "latency", project="/a", kind="decision")
    semantic = _memory(conn, "speed", "throughput", project="/a", kind="decision")
    wrong_project = _memory(conn, "other", "throughput", project="/b", kind="decision")
    wrong_kind = _memory(conn, "note", "throughput", project="/a", kind="note")
    for memory_id, title, content in ((both, "latency", "latency"), (semantic, "speed", "throughput"),
                                      (wrong_project, "other", "throughput"), (wrong_kind, "note", "throughput")):
        _embedding(conn, memory_id, title, content, "", (1, 0))
    fake_embedder(FakeEmbedder(queries={"latency": _vector(1, 0)}))

    rows = hybrid_search(conn, "latency", where_sql="m.project=?", params=("/a",), kind="decision", limit=8)

    assert [row["id"] for row in rows] == [both, semantic]
    assert [row["match"] for row in rows] == ["both", "semantic"]


def test_disabled_or_failed_embeddings_are_plain_bm25(tmp_path, monkeypatch):
    conn = connect(tmp_path / "memory.db")
    first = _memory(conn, "alpha", "alpha")
    second = _memory(conn, "other", "alpha")
    monkeypatch.setenv("DEVMEM_EMBED", "0")
    fake = FakeEmbedder(raises=True)
    set_embedder(fake)
    try:
        rows = hybrid_search(conn, "alpha", where_sql="m.project=?", params=("/project",), limit=8)
        assert [row["id"] for row in rows] == [first, second]
        assert fake.calls == []
    finally:
        set_embedder(None)


def test_ensure_refreshes_and_delete_trigger_removes_embedding(tmp_path, fake_embedder):
    conn = connect(tmp_path / "memory.db")
    memory_id = _memory(conn, "first", "one")
    fake = FakeEmbedder(documents={"first\none\n": _vector(1, 0), "first\ntwo\n": _vector(0, 1)})
    fake_embedder(fake)

    assert ensure_embeddings(conn) == {"embedded": 1, "pending": 0}
    old_hash = conn.execute("SELECT content_hash FROM memory_embeddings WHERE memory_id=?", (memory_id,)).fetchone()[0]
    conn.execute("UPDATE memories SET content='two' WHERE id=?", (memory_id,))
    assert ensure_embeddings(conn) == {"embedded": 1, "pending": 0}
    assert conn.execute("SELECT content_hash FROM memory_embeddings WHERE memory_id=?", (memory_id,)).fetchone()[0] != old_hash
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    assert conn.execute("SELECT * FROM memory_embeddings WHERE memory_id=?", (memory_id,)).fetchone() is None


def _drop_v4(conn):
    conn.execute("DROP TRIGGER memories_revisions_bu")
    conn.execute("DROP TRIGGER memories_revisions_bd")
    conn.execute("DROP TABLE memory_revisions")
    conn.execute("ALTER TABLE memories DROP COLUMN pinned")


def _drop_v5(conn):
    conn.execute("DROP TRIGGER memories_links_ad")
    conn.execute("DROP TRIGGER memory_links_revisions_bd")
    conn.execute("DROP TABLE memory_link_revisions")
    conn.execute("DROP TABLE memory_links")


def test_v2_database_migrates_to_memory_embeddings(tmp_path):
    path = tmp_path / "previous.db"
    conn = connect(path)
    _drop_v5(conn)
    _drop_v4(conn)
    conn.execute("DROP TRIGGER memories_embeddings_ad")
    conn.execute("DROP TABLE memory_embeddings")
    conn.execute("UPDATE schema_version SET version=2")
    conn.commit()
    conn.close()

    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    assert migrated.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_embeddings'").fetchone()
    assert migrated.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='memories_embeddings_ad'").fetchone()


def test_v2_to_v3_migration_preserves_memories_fts_and_is_idempotent(tmp_path):
    path = tmp_path / "v2.db"
    conn = connect(path)
    memory_id = _memory(conn, "Needle title", "needle body", tags="tag")
    _drop_v5(conn)
    _drop_v4(conn)
    conn.execute("DROP TRIGGER memories_embeddings_ad")
    conn.execute("DROP TABLE memory_embeddings")
    conn.execute("UPDATE schema_version SET version=2")
    conn.commit(); conn.close()
    migrated = connect(path)
    assert migrated.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert migrated.execute("SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'needle'").fetchone()[0] == memory_id
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    migrated.close()
    again = connect(path)
    assert again.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert again.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0


def test_min_cosine_boundary_excludes_point_59_and_keeps_point_61(tmp_path, fake_embedder):
    conn = connect(tmp_path / "boundary.db")
    excluded = _memory(conn, "excluded", "unrelated")
    included = _memory(conn, "included", "also unrelated")
    _embedding(conn, excluded, "excluded", "unrelated", "", (.59, (1 - .59 ** 2) ** .5))
    _embedding(conn, included, "included", "also unrelated", "", (.61, (1 - .61 ** 2) ** .5))
    fake_embedder(FakeEmbedder(queries={"needle": _vector(1, 0)}))
    rows = hybrid_search(conn, "needle", where_sql="m.project=?", params=("/project",))
    assert [(row["id"], row["match"]) for row in rows] == [(included, "semantic")]


@pytest.mark.parametrize("vectors", [np.zeros((1, EMBEDDING_DIM), dtype=np.float32), np.zeros((1, 2), dtype=np.float32)])
def test_ensure_embeddings_rejects_zero_and_wrong_shape_vectors(tmp_path, fake_embedder, vectors):
    conn = connect(tmp_path / "invalid.db")
    _memory(conn, "one", "body")
    fake_embedder(FakeEmbedder(documents={"one\nbody\n": vectors[0]}))
    assert ensure_embeddings(conn) == {"embedded": 0, "pending": 1}
    assert conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0


def test_ensure_embeddings_failure_limit_and_disappearing_memory_are_safe(tmp_path, fake_embedder):
    conn = connect(tmp_path / "ensure.db")
    ids = [_memory(conn, f"t{i}", f"c{i}") for i in range(3)]
    fake_embedder(FakeEmbedder(raises=True))
    assert ensure_embeddings(conn) == {"embedded": 0, "pending": 3}
    assert conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
    vectors = {f"t{i}\nc{i}\n": _vector(1, 0) for i in range(3)}
    batching = FakeEmbedder(documents=vectors)
    fake_embedder(batching)
    assert ensure_embeddings(conn, limit=2) == {"embedded": 2, "pending": 1}
    assert [texts for texts, query in batching.calls if not query] == [["t0\nc0\n", "t1\nc1\n"]]

    class DeletesDuringEmbed(FakeEmbedder):
        def embed(self, texts, *, query=False):
            conn.execute("DELETE FROM memories WHERE id=?", (ids[2],))
            return super().embed(texts, query=query)

    fake_embedder(DeletesDuringEmbed(documents={"t2\nc2\n": _vector(1, 0)}))
    assert ensure_embeddings(conn) == {"embedded": 0, "pending": 0}


def test_hybrid_search_returns_keyword_results_when_embedding_backfill_locks(tmp_path, monkeypatch):
    import devmem.db as database
    conn = connect(tmp_path / "locked.db")
    memory_id = _memory(conn, "needle", "needle")
    monkeypatch.delenv("DEVMEM_EMBED", raising=False)
    monkeypatch.setattr(database, "ensure_embeddings", lambda conn: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    set_embedder(FakeEmbedder(raises=True))
    try: rows = hybrid_search(conn, "needle", where_sql="m.project=?", params=("/project",))
    finally: set_embedder(None)
    assert [row["id"] for row in rows] == [memory_id]


def test_embed_command_fails_with_reason_when_pending_cannot_be_embedded(tmp_path, monkeypatch, capsys):
    from devmem import cli
    conn = connect(tmp_path / "embed.db")
    _memory(conn, "pending", "body")
    conn.commit(); conn.close()
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "embed.db"))
    set_embedder(FakeEmbedder(raises=True))
    try:
        monkeypatch.setattr(__import__("sys"), "argv", ["devmem", "embed"])
        with pytest.raises(SystemExit) as exc: cli.main()
    finally:
        set_embedder(None)
    captured = capsys.readouterr()
    assert exc.value.code == 1 and "1 pending" in captured.out and "embedder unavailable" in captured.err
