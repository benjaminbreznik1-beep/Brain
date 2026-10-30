from __future__ import annotations

import sys
import sqlite3

import pytest
from fastapi.testclient import TestClient

from devmem import cli, db, mcp_server
from devmem.db import connection, save_memory, task_board, task_status_set
from devmem.panel import app as panel_app
from devmem.panel.app import app


BOARD = ("NOW: 1. 3.1 parse off the event loop — MERGED !19306 → PHASE: merged 26 Oct; follow-up PR not started. "
         "2. 2.2 LibreOffice import → PHASE: done 26 Oct. NEXT: 3. 3.2 Ship it → PHASE: queued. "
         "LATER: 4. 4.1 Tidy docs → PHASE: later. Status as of 30 Oct 2026.")


def _memory(conn, title, content, *, tags="", project="/work/demo"):
    return save_memory(conn, title=title, content=content, tags=tags, project=project, kind="todo")


def _board(conn, content=BOARD, *, project="/work/demo"):
    return _memory(conn, "Task board", content, tags="board", project=project)


@pytest.fixture(autouse=True)
def _fresh_mcp_client(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_id", None)
    monkeypatch.setattr(mcp_server, "_client_name", None)


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


def test_parser_section_adjacency_dates_and_missing_entries(monkeypatch):
    entries, status = db.parse_task_board("step 1. review. 26. 10. 2026. NOW:1. 3.1 x → PHASE: ready. Status as of 30.10.2026.")
    assert [(entry["section"], entry["n"], entry["task"]) for entry in entries] == [("NOW", 1, "3.1")]
    assert status == "30.10.2026"
    monkeypatch.setattr(db, "_board_today", lambda: ("5 Nov 2026", "5 Nov"))
    with connection() as conn:
        board = _board(conn, "NOW: 1. 3.1 x → PHASE: old. Status as of yesterday.")
        assert task_status_set(conn, board, "3.1", "done 31 Oct")["board"]["new_line"] == "1. 3.1 x → PHASE: done 31 Oct"
        assert "Status as of 5 Nov 2026." in conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0]
        empty = _board(conn, "Status as of yesterday.")
        assert task_board(conn, empty) == {"ok": False, "error": f"no board entries found in #{empty}"}


def test_set_replaces_phase_appends_missing_phase_and_updates_status_date():
    with connection() as conn:
        board = _board(conn, "NOW: 1. 2.2 Work — PHASE: old. NEXT: 2. 3.1 More. Status as of yesterday.")
        changed = task_status_set(conn, board, "2.2", "done today")
        appended = task_status_set(conn, board, "3.1", "queued")
        content = conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0]
    assert changed["ok"] and "→ PHASE: done today" in changed["board"]["new_line"]
    assert appended["ok"] and "More → PHASE: queued" in content
    assert f"Status as of {db._board_today()[0]}." in content


def test_phase_replacement_is_the_only_board_entry_change(monkeypatch):
    monkeypatch.setattr(db, "_board_today", lambda: ("5 Nov 2026", "5 Nov"))
    original = "NOW: 1. 3.1 One → PHASE: old. NEXT: 2. 3.2 Two → PHASE: queued. Status as of yesterday."
    with connection() as conn:
        board = _board(conn, original)
        result = task_status_set(conn, board, "3.1", "done 31 Oct.")
        stored = conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0]
    expected = "NOW: 1. 3.1 One → PHASE: done 31 Oct. NEXT: 2. 3.2 Two → PHASE: queued. Status as of 5 Nov 2026."
    assert result["board"] == {"id": board, "old_line": "1. 3.1 One → PHASE: old.", "new_line": "1. 3.1 One → PHASE: done 31 Oct."}
    assert stored == expected and stored.count("→ PHASE:") == original.count("→ PHASE:")


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


def test_expected_failures_leave_content_and_revisions_unchanged(monkeypatch):
    monkeypatch.setattr(db, "_board_today", lambda: ("5 Nov 2026", "5 Nov"))
    with connection() as conn:
        board = _board(conn, "NOW: 1. 2.2 Work → PHASE: old. 2. 2.2 Duplicate → PHASE: old. Status as of yesterday.")
        entry = _memory(conn, "entry", "body")
        before = {row["id"]: row["content"] for row in conn.execute("SELECT id,content FROM memories")}
        failures = [
            (99999, "2.2", "done", None, None, "board not found"),
            (board, "2.2", "done", None, None, "expected exactly one board entry for 2.2, found 2"),
            (board, "3.1", "done", None, None, "expected exactly one board entry for 3.1, found 0"),
        ]
        for bid, task, phase, status, entry_id, error in failures:
            result = task_status_set(conn, bid, task, phase, status=status, entry_id=entry_id)
            assert result == {"ok": False, "error": error}
        assert {row["id"]: row["content"] for row in conn.execute("SELECT id,content FROM memories")} == before
        assert task_status_set(conn, board, "2.2", "done", entry_id=board) == {"ok": False, "error": "entry_id must differ from board_id"}
    with connection() as fresh:
        assert fresh.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 0

    with connection() as conn:
        board = _board(conn, "NOW: 1. 2.2 Work → PHASE: old. Status as of yesterday.")
        before = conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0]
        assert task_status_set(conn, board, "2.2", "x" * 2100) == {"ok": False, "error": "content exceeds the 2000 character limit; put long material in a file and save a short summary plus the file path"}
        assert task_status_set(conn, board, "2.2", "ghp_" + "A" * 36) == {"ok": False, "error": "secret detected"}
        assert task_status_set(conn, board, "2.2", "done", status="ghp_" + "A" * 36, entry_id=entry) == {"ok": False, "error": "secret detected"}
        assert task_status_set(conn, board, "2.2", "done", status="x", entry_id=99998) == {"ok": False, "error": "entry memory not found"}
        assert conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0] == before


def test_dry_run_matches_real_run_and_does_not_create_revisions(monkeypatch):
    monkeypatch.setattr(db, "_board_today", lambda: ("5 Nov 2026", "5 Nov"))
    with connection() as conn:
        board = _board(conn, "NOW: 1. 2.2 Work → PHASE: old. Status as of yesterday.")
        entry = _memory(conn, "Phase 2 task 2.2 Work", "body")
        preview = task_status_set(conn, board, "2.2", "done", status="shipped", dry_run=True)
        assert preview["entry"]["new_status"] == "STATUS (5 Nov): shipped"
        assert conn.execute("SELECT COUNT(*) FROM memory_revisions WHERE memory_id IN (?,?)", (board, entry)).fetchone()[0] == 0
        actual = task_status_set(conn, board, "2.2", "done", status="shipped")
        assert actual == preview
        assert conn.execute("SELECT COUNT(*) FROM memory_revisions WHERE memory_id IN (?,?)", (board, entry)).fetchone()[0] == 2


def test_entry_lookup_scope_prefix_and_manual_supersedes():
    with connection() as conn:
        board = _board(conn, project=None)
        wrong = _memory(conn, "Phase 2 task 2.21 wrong", "body", project=None)
        chosen = _memory(conn, "custom 2.2 selected", "body", project=None)
        other = _memory(conn, "Custom 2.2 elsewhere", "body", project="/other")
        hidden = _memory(conn, "Custom 2.2 hidden", "body", project=None)
        replacement = _memory(conn, "replacement", "body", project=None)
        conn.execute("INSERT INTO memory_links(from_id,to_id,type,note,created_at) VALUES(?,?,?,?,?)", (replacement, hidden, "supersedes", "", db.now()))
        result = task_status_set(conn, board, "2.2", "done", status="new", entry_title_prefix="Custom")
    assert result["entry"]["id"] == chosen and wrong != chosen and other != chosen


def test_no_status_does_not_touch_entry_and_unexpected_errors_reraise(monkeypatch):
    with connection() as conn:
        board = _board(conn, "NOW: 1. 2.2 Work → PHASE: old. Status as of yesterday.")
        entry = _memory(conn, "Phase 2 task 2.2 Work", "body")
        assert task_status_set(conn, board, "2.2", "done")["entry"] is None
        assert conn.execute("SELECT content FROM memories WHERE id=?", (entry,)).fetchone()[0] == "body"
        monkeypatch.setattr(db, "update_memory", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write exploded")))
        with pytest.raises(RuntimeError, match="write exploded"):
            task_status_set(conn, board, "2.2", "again")


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
    output = capsys.readouterr().out
    assert "2. 2.2 LibreOffice import → PHASE: done" in output
    with connection() as conn:
        assert "2. 2.2 LibreOffice import → PHASE: done" in conn.execute("SELECT content FROM memories WHERE id=?", (board,)).fetchone()[0]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        page = client.get("/overview").text
    assert "Task board" in page and f'href="/memory/{board}"' in page and "LibreOffice import" in page


def test_mcp_wrappers_log_expected_failure_and_task_board_result():
    with connection() as conn:
        board = _board(conn)
    assert mcp_server.task_board(board)["entries"][0]["task"] == "3.1"
    assert mcp_server.task_status_set(board, "missing", "done") == {"ok": False, "error": "expected exactly one board entry for missing, found 0"}
    with connection() as conn:
        call = conn.execute("SELECT ok,error FROM tool_calls WHERE tool='task_status_set' ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(call) == (0, "expected exactly one board entry for missing, found 0")


def test_panel_memory_rules_and_superseded_boards(monkeypatch, caplog):
    with connection() as conn:
        memory = _memory(conn, "Visible", "body")
        board = _board(conn)
        replacement = _memory(conn, "replacement", "body")
        conn.execute("INSERT INTO memory_links(from_id,to_id,type,note,created_at) VALUES(?,?,?,?,?)", (replacement, board, "supersedes", "", db.now()))
        plural_tag = _memory(conn, "Not a board", BOARD, tags="boards")
        _memory(conn, "Rules", 'GUARD: {"tool":"bash","action":"ask"}')
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get(f"/memory/{memory}").status_code == 200
        assert client.get("/memory/999999").status_code == 404
        assert "did you mean Bash?" in client.get("/").text
        overview = client.get("/overview").text
    assert f'href="/memory/{board}"' not in overview and f'href="/memory/{plural_tag}"' not in overview
    monkeypatch.setattr(panel_app, "rules_data", lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("rules broken")))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        unavailable = client.get("/").text
    assert "Rules unavailable: rules broken" in unavailable
    assert "rules loading failed" in caplog.text
