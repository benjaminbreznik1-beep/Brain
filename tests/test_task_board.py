from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from devmem import cli, db
from devmem.db import connection, save_memory, task_board, task_status_set
from devmem.panel.app import app


BOARD = ("NOW: 1. 3.1 parse off the event loop — MERGED !19306 → PHASE: merged 26 Oct; follow-up PR not started. "
         "2. 2.2 LibreOffice import → PHASE: done 26 Oct. NEXT: 3. 3.2 Ship it → PHASE: queued. "
         "LATER: 4. 4.1 Tidy docs → PHASE: later. Status as of 30 Oct 2026.")


def _memory(conn, title, content, *, tags="", project="/work/demo"):
    return save_memory(conn, title=title, content=content, tags=tags, project=project, kind="todo")


def _board(conn, content=BOARD):
    return _memory(conn, "Task board", content, tags="board")


def test_parser_handles_one_paragraph_sections_and_task_boundaries():
    entries, status = db.parse_task_board(BOARD)
    assert [(x["section"], x["n"], x["task"], x["title"], x["phase"]) for x in entries] == [
        ("NOW", 1, "3.1", "parse off the event loop — MERGED !19306", "merged 26 Oct; follow-up PR not started."),
        ("NOW", 2, "2.2", "LibreOffice import", "done 26 Oct."),
        ("NEXT", 3, "3.2", "Ship it", "queued."), ("LATER", 4, "4.1", "Tidy docs", "later.")]
    assert status == "30 Oct 2026"
    with connection() as conn:
        board = _board(conn, "NOW: 1. 12.2 twelve → PHASE: old. 2. 2.21 twenty-one → PHASE: old. 3. 2.2 exact → PHASE: old.")
        assert task_status_set(conn, board, "2.2", "new")["board"]["old_line"].startswith("3. 2.2")


def test_set_replaces_phase_appends_missing_phase_and_updates_status_date():
    with connection() as conn:
        board = _board(conn, "NOW: 1. 2.2 Work — PHASE: old. NEXT: 2. 3.1 More. Status as of yesterday.")
        changed = task_status_set(conn, board, "2.2", "done today")
        appended = task_status_set(conn, board, "3.1", "queued")
        content = conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0]
    assert changed["ok"] and "→ PHASE: done today" in changed["board"]["new_line"]
    assert appended["ok"] and "More → PHASE: queued" in content
    assert f"Status as of {db._board_today()[0]}." in content


def test_status_entry_lookup_replace_prepend_explicit_id_and_superseded_ignored():
    with connection() as conn:
        board = _board(conn)
        entry = _memory(conn, "Phase 2 task 2.2 LibreOffice", "STATUS (1 Jan): old\nbody")
        result = task_status_set(conn, board, "2.2", "done", status="new")
        assert result["entry"]["id"] == entry and result["entry"]["old_status"] == "STATUS (1 Jan): old"
        other = _memory(conn, "anything", "body")
        explicit = task_status_set(conn, board, "3.1", "merged", status="fresh", entry_id=other)
        assert explicit["entry"]["id"] == other
        hidden = _memory(conn, "Phase 2 task 4.1 Tidy", "old")
        replacement = _memory(conn, "replacement", "new")
        active = _memory(conn, "Phase 2 task 4.1 Current", "body")
        conn.execute("INSERT INTO memory_links(from_id,to_id,type,note,created_at) VALUES(?,?,?,?,?)", (replacement, hidden, "supersedes", "automatic maintenance", db.now()))
        assert task_status_set(conn, board, "4.1", "later", status="later")["entry"]["id"] == active
        assert conn.execute("SELECT content FROM memories WHERE id=?", (other,)).fetchone()[0].startswith("STATUS (")


@pytest.mark.parametrize("count", [0, 2])
def test_entry_lookup_requires_exactly_one_non_superseded_memory(count):
    with connection() as conn:
        board = _board(conn)
        for suffix in range(count): _memory(conn, f"Phase 2 task 2.2 item {suffix}", "body")
        result = task_status_set(conn, board, "2.2", "done", status="new")
    assert result == {"ok": False, "error": f"expected exactly one entry memory for 2.2, found {count}"}


def test_atomic_dry_run_history_and_validation_failures(monkeypatch):
    with connection() as conn:
        board = _board(conn)
        entry = _memory(conn, "Phase 2 task 2.2 LibreOffice", "body")
        before = conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0]
        assert task_status_set(conn, board, "2.2", "done", status="new", dry_run=True)["ok"]
        assert conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0] == before
        real_update = db.update_memory
        monkeypatch.setattr(db, "update_memory", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("second write failed")) if args[1] == entry else real_update(*args, **kwargs))
        assert task_status_set(conn, board, "2.2", "done", status="new")["ok"] is False
        assert conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0] == before
    with connection() as conn:
        monkeypatch.undo()
        assert task_status_set(conn, board, "2.2", "done", status="new")["ok"]
        assert conn.execute("SELECT COUNT(*) FROM memory_revisions WHERE memory_id IN (?,?)", (board, entry)).fetchone()[0] == 2


def test_read_tool_cli_and_panel_board_card(monkeypatch, capsys):
    with connection() as conn:
        board = _board(conn)
        _memory(conn, "Phase 2 task 2.2 LibreOffice", "body")
        parsed = task_board(conn, board)
    assert parsed["entries"][1]["task"] == "2.2"
    monkeypatch.setattr(sys, "argv", ["devmem", "board", "show", str(board)])
    cli.main()
    assert "NOW\t2\t2.2" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["devmem", "board", "set", str(board), "2.2", "--phase", "done", "--status", "new"])
    cli.main()
    assert "→ PHASE: done" in capsys.readouterr().out
    with TestClient(app, base_url="http://127.0.0.1") as client:
        page = client.get("/overview").text
    assert "Task board" in page and f'href="/memory/{board}"' in page and "LibreOffice import" in page
