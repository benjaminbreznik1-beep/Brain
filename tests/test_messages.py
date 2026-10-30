"""Agent messages (schema v9, MCP tools, panel) and the live subagent scanner, on temp DBs and a temp ~/.claude only."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devmem import db, maintenance, mcp_server, messages
from devmem.db import SCHEMA_VERSION, connect, connection, now
from devmem.ingest import runner, subagents
from devmem.ingest.runner import ingest_all

SESSION = "11111111-2222-3333-4444-555555555555"
SECRET_CMD = "curl -H 'Authorization: Bearer sk-ant-abcdefghijklmnopqrstuvwxyz0123' https://example.invalid && " + "x" * 200


@pytest.fixture(autouse=True)
def _fresh_mcp_client(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_id", None); monkeypatch.setattr(mcp_server, "_client_name", None)


def _ts(minutes_ago=0.0):
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _line(kind, content, minutes_ago, agent, stop=None, **extra):
    return json.dumps({"type": kind, "isSidechain": True, "agentId": agent, "sessionId": SESSION, "cwd": "/work/demo", "timestamp": _ts(minutes_ago),
                       "message": {"role": kind, "content": content, **({"id": f"m{minutes_ago}{agent}", "model": "claude-x", "stop_reason": stop,
                                   "usage": {"input_tokens": 10, "output_tokens": 5}} if kind == "assistant" else {})}, **extra})


def _subagent(root: Path, agent: str, lines: list[str], meta: dict | None):
    folder = root / "projects" / "-work-demo" / SESSION / "subagents"; folder.mkdir(parents=True, exist_ok=True)
    (folder / f"agent-{agent}.jsonl").write_text("\n".join(lines) + "\n")
    if meta is not None: (folder / f"agent-{agent}.meta.json").write_text(json.dumps(meta))
    return folder


def _two_agents(root: Path, running_minutes_ago=1.0):
    done = [
        _line("user", "Review the diff in /work/demo and report findings. Use token = abcdefghijklmnop", 5, "aaa"),
        _line("user", "<system-reminder>later user text is not the task</system-reminder>", 5, "aaa"),
        _line("assistant", [{"type": "thinking", "thinking": "private chain of thought"},
                            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": SECRET_CMD, "description": "fetch"}}], 4.5, "aaa"),
        _line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "TOOL-RESULT-CONTENT"}], 4.4, "aaa"),
        _line("assistant", [{"type": "text", "text": "Reading a.py next."}], 4.2, "aaa", stop="tool_use"),
        _line("assistant", [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/work/demo/a.py"}}], 4, "aaa", stop="tool_use"),
        _line("assistant", [{"type": "text", "text": "Found 2 issues: a.py:3 and a.py:9. Key ghp_" + "Q" * 36}], 3, "aaa", stop="end_turn"),
    ]
    running = [
        _line("user", "Hunt silent failures.", running_minutes_ago + 1, "bbb"),
        _line("assistant", [{"type": "tool_use", "id": "t3", "name": "Grep", "input": {"pattern": "except Exception", "path": "/work/demo"}}], running_minutes_ago, "bbb"),
    ]
    _subagent(root, "aaa", done, {"agentType": "pr-review-toolkit:code-reviewer", "description": "Review diff", "toolUseId": "toolu_1", "spawnDepth": 1, "finished": True})
    return _subagent(root, "bbb", running, {"agentType": "pr-review-toolkit:silent-failure-hunter", "description": "Silent failures", "toolUseId": "toolu_2", "spawnDepth": 1, "finished": False})


def _post(body="hello", **kw):
    with connection() as conn:
        return messages.post(conn, body, sender=kw.pop("sender", "alpha"), sender_kind=kw.pop("sender_kind", "claude"), **kw)


# ---------------------------------------------------------------- Part A

def _claude_home() -> Path:
    home = Path(os.environ["DEVMEM_CLAUDE_HOME"])
    assert "pytest" in str(home)  # conftest keeps the scanner off the real ~/.claude
    return home


def _meta(agent, finished=False, agent_type="general-purpose", description=None):
    return {"agentType": agent_type, "description": description or f"Task {agent}", "toolUseId": f"toolu_{agent}", "spawnDepth": 1, "finished": finished}


def _event(conn, agent, prefix):
    if prefix == "started:": return conn.execute("SELECT * FROM messages WHERE agent_id=? AND subject LIKE 'dispatched %'", (agent,)).fetchone()
    if prefix == "report:": return conn.execute("SELECT * FROM messages WHERE agent_id=? AND subject NOT LIKE 'dispatched %'", (agent,)).fetchone()
    if prefix == "stopped:": return conn.execute("SELECT * FROM messages WHERE agent_id=? AND subject LIKE '%stopped without a report%'", (agent,)).fetchone()
    return conn.execute("SELECT * FROM messages WHERE agent_id=? AND subject LIKE ?", (agent, prefix + "%")).fetchone()


def test_scanner_reads_subagents_tools_remarks_reports_and_status():
    _two_agents(_claude_home())
    ingest_all()
    with connection() as conn:
        done, running = messages.subagent(conn, "aaa"), messages.subagent(conn, "bbb")
        assert done["agent_type"] == "pr-review-toolkit:code-reviewer" and done["session_id"] == SESSION and done["project"] == "/work/demo"
        assert done["task"] == "Review the diff in /work/demo and report findings. Use token = [redacted]"
        assert [t["name"] for t in done["tools"]] == ["Bash", "Read"] and done["tools"][1]["arg"] == "/work/demo/a.py"
        bash = done["tools"][0]["arg"]
        assert bash.startswith("curl -H 'Authorization: [redacted]") and len(bash) <= 80 and "sk-ant" not in bash
        assert [(t["role"], t["body"]) for t in done["texts"]] == [("agent", "Reading a.py next."), ("agent", "Found 2 issues: a.py:3 and a.py:9. Key [redacted]")]
        assert done["status"] == "finished" and done["report"] == "Found 2 issues: a.py:3 and a.py:9. Key [redacted]" and done["tokens"] == 60
        assert running["status"] == "running" and running["report"] is None and running["tools"] == [{"ts": running["tools"][0]["ts"], "name": "Grep", "arg": "except Exception"}]
        dump = "\n".join(conn.iterdump())
        for leaked in ("TOOL-RESULT-CONTENT", "private chain of thought", "sk-ant-abc", "abcdefghijklmnop", "ghp_QQQQ", "later user text"):
            assert leaked not in dump
        events = {(e["agent_id"], e["subject"]): e for e in conn.execute("SELECT agent_id,kind,subject,recipient,status,thread_id,reply_to FROM messages")}
        assert set(events) == {("aaa", "dispatched code reviewer — Review diff"), ("bbb", "dispatched bug hunter — Silent failures"), ("aaa", "code reviewer: 2 issues found")}
        assert {e["kind"] for e in events.values()} == {"event"} and {e["recipient"] for e in events.values()} == {"#agents"}
        spawn, report = events[("aaa", "dispatched code reviewer — Review diff")], events[("aaa", "code reviewer: 2 issues found")]
        assert report["thread_id"] == spawn["thread_id"] and report["reply_to"] == spawn["thread_id"]
        assert (spawn["status"], report["status"], events[("bbb", "dispatched bug hunter — Silent failures")]["status"]) == ("closed", "closed", "open")
        listed = messages.session_subagents(conn, [SESSION])[SESSION]
        assert [a["agent_id"] for a in listed] == ["bbb", "aaa"]  # newest activity first
    ingest_all()  # second pass: nothing duplicated
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3 and conn.execute("SELECT COUNT(*) FROM subagent_tools").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM subagent_texts").fetchone()[0] == 2


def test_quiet_agent_goes_stale_then_ends_on_its_end_turn_text_without_meta():
    folder = _two_agents(_claude_home(), running_minutes_ago=20)
    ingest_all()
    with connection() as conn: assert messages.subagent(conn, "bbb")["status"] == "stale"
    with (folder / "agent-bbb.jsonl").open("a") as f: f.write(_line("assistant", [{"type": "text", "text": "No silent failures."}], 0, "bbb", stop="end_turn") + "\n")
    ingest_all()  # the sidecar still says finished: false
    with connection() as conn:
        agent = messages.subagent(conn, "bbb")
        assert agent["status"] == "finished" and agent["report"] == "No silent failures." and agent["ended"] == "finished"
        assert tuple(conn.execute("SELECT body,status FROM messages WHERE id=?", (agent["report_msg_id"],)).fetchone()) == ("No silent failures.", "closed")


def test_handback_ends_the_run_and_is_the_report():
    lines = [_line("user", "Fix it.", 3, "hhh"),
             _line("assistant", [{"type": "text", "text": "Working on it."}], 2.5, "hhh", stop="tool_use"),
             _line("assistant", [{"type": "tool_use", "id": "h1", "name": "SubagentHandback", "input": {"message": "Fixed in db.py."}}], 2, "hhh", stop="tool_use"),
             _line("user", [{"type": "tool_result", "tool_use_id": "h1", "content": "ok"}], 2, "hhh", toolEndsTurn=True)]
    _subagent(_claude_home(), "hhh", lines, _meta("hhh"))
    ingest_all()
    with connection() as conn:
        agent = messages.subagent(conn, "hhh")
        assert agent["status"] == "finished" and agent["report"] == "Fixed in db.py." and agent["last_terminal"] == 1
        assert _event(conn, "hhh", "report:")["body"] == "Fixed in db.py."


def test_report_waits_for_an_unterminated_last_line():
    folder = _subagent(_claude_home(), "ppp", [_line("user", "Go.", 3, "ppp"),
                                               _line("assistant", [{"type": "text", "text": "mid-run note"}], 2, "ppp", stop="tool_use")], _meta("ppp", finished=True))
    final = _line("assistant", [{"type": "tool_use", "id": "h", "name": "SubagentHandback", "input": {"message": "The real report."}}], 0, "ppp", stop="tool_use")
    with (folder / "agent-ppp.jsonl").open("a") as f: f.write(final[:40])  # meta says finished, the handback line is half written
    ingest_all()
    with connection() as conn: assert _event(conn, "ppp", "report:") is None and messages.subagent(conn, "ppp")["tail_partial"] == 1
    with (folder / "agent-ppp.jsonl").open("a") as f: f.write(final[40:] + "\n")
    ingest_all()
    with connection() as conn: assert _event(conn, "ppp", "report:")["body"] == "The real report."


def test_meta_finished_without_end_record_waits_report_wait_s():
    _subagent(_claude_home(), "www", [_line("user", "Go.", 0.2, "www"),
                                      _line("assistant", [{"type": "tool_use", "id": "x", "name": "Read", "input": {"file_path": "a"}}], 0.1, "www", stop="tool_use")],
              _meta("www", finished=True))
    ingest_all()
    with connection() as conn:
        assert _event(conn, "www", "report:") is None  # within REPORT_WAIT_S of the last write
        conn.execute("UPDATE subagents SET last_activity_at=? WHERE agent_id='www'", ((datetime.now(UTC) - timedelta(seconds=subagents.REPORT_WAIT_S + 5)).isoformat(),))
    ingest_all()
    with connection() as conn: assert _event(conn, "www", "report:")["body"] == "(finished without a text report)"


def test_silent_agent_is_stopped_after_the_cutoff_and_its_thread_closed():
    _subagent(_claude_home(), "sss", [_line("user", "Go.", 200, "sss"),
                                      _line("assistant", [{"type": "text", "text": "Halfway there."}], 190, "sss", stop="tool_use")], _meta("sss"))
    _subagent(_claude_home(), "nnn", [_line("user", "Go.", 180, "nnn")], _meta("nnn"))
    ingest_all()
    with connection() as conn:
        assert messages.subagent(conn, "sss")["status"] == "stopped" and messages.subagent(conn, "sss")["ended"] == "stopped"
        stopped = _event(conn, "sss", "stopped:")
        assert stopped["body"].startswith("(no report: agent stopped") and "Halfway there." in stopped["body"]
        assert _event(conn, "nnn", "stopped:")["body"] == "(no report: agent stopped)"
        assert {r[0] for r in conn.execute("SELECT DISTINCT status FROM messages")} == {"closed"}
    ingest_all()
    with connection() as conn: assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4  # once


def test_report_for_a_pruned_spawn_thread_is_skipped():
    _subagent(_claude_home(), "zzz", [_line("user", "Go.", 1, "zzz")], _meta("zzz"))
    ingest_all()
    with connection() as conn:
        conn.execute("DELETE FROM messages")
        conn.execute("UPDATE subagents SET finished=1, last_activity_at=? WHERE agent_id='zzz'", ((datetime.now(UTC) - timedelta(hours=1)).isoformat(),))
    ingest_all()
    with connection() as conn:
        assert conn.execute("SELECT report_msg_id FROM subagents WHERE agent_id='zzz'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_bad_lines_and_files_never_freeze_the_ingest_pass(monkeypatch, caplog):
    home = _claude_home()
    _two_agents(home)
    odd = [json.dumps({"type": "assistant", "sessionId": {"x": 1}, "cwd": 5, "timestamp": ["no"], "message": {"role": "assistant", "content": [{"type": "text", "text": "typed oddly"}]}})]
    _subagent(home, "odd", odd, _meta("odd"))
    real_apply = subagents.apply
    def explode(conn, aid, rec, offset):
        if aid == "bbb": raise sqlite3.ProgrammingError("unsupported type")
        return real_apply(conn, aid, rec, offset)
    monkeypatch.setattr(subagents, "apply", explode)
    with caplog.at_level("WARNING", "devmem.ingest"):
        ingest_all(); ingest_all()
    with connection() as conn:
        assert messages.subagent(conn, "odd")["session_id"] == SESSION  # from the folder, not the dict
        assert messages.subagent(conn, "aaa")["report"] and conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] > 0
        assert conn.execute("SELECT byte_offset FROM subagents WHERE agent_id='bbb'").fetchone()[0] == 0  # rolled back, retried next pass
    failures = [r for r in caplog.records if "agent-bbb.jsonl" in r.getMessage()]
    assert len(failures) == 1 and "at byte 0" in failures[0].getMessage()  # logged once with path and offset
    monkeypatch.setattr(subagents, "apply", real_apply)
    real_read = runner._read_new
    def vanished(path, offset):
        if path.name == "agent-bbb.jsonl": raise FileNotFoundError(path)
        return real_read(path, offset)
    monkeypatch.setattr(runner, "_read_new", vanished)
    ingest_all()  # removed between stat and open: skipped quietly
    monkeypatch.setattr(runner, "_read_new", real_read)
    ingest_all()
    with connection() as conn: assert messages.subagent(conn, "bbb")["tools"][0]["name"] == "Grep"


def test_corrupt_sidecar_is_logged_once_per_version(caplog):
    folder = _subagent(_claude_home(), "bad", [_line("user", "Go.", 1, "bad")], None)
    meta = folder / "agent-bad.meta.json"; meta.write_text("{not json")
    with caplog.at_level("WARNING", "devmem.ingest"):
        ingest_all(); ingest_all()
        assert len([r for r in caplog.records if "agent-bad.meta.json" in r.getMessage()]) == 1
        meta.write_text("[1, 2]"); os.utime(meta, (5, 5)); ingest_all()
        assert len([r for r in caplog.records if "agent-bad.meta.json" in r.getMessage()]) == 2


def test_prune_drops_subagent_timelines_with_their_thread():
    folder = _two_agents(_claude_home())
    ingest_all()
    long_ago = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    with connection() as conn:
        conn.execute("UPDATE messages SET created_at=?, closed_at=? WHERE agent_id='aaa'", (long_ago, long_ago))
        assert messages.prune(conn) == 2
        row = conn.execute("SELECT * FROM subagents WHERE agent_id='aaa'").fetchone()
        assert row["spawn_msg_id"] == -1 and row["task"] is None and messages.subagent(conn, "aaa")["thread_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM subagent_tools WHERE agent_id='aaa'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM subagent_texts WHERE agent_id='aaa'").fetchone()[0] == 0
    ingest_all()  # the tombstone keeps the scanner from posting the agent again
    with connection() as conn: assert _event(conn, "aaa", "started:") is None
    assert "thread" not in _client().get("/messages/agent/aaa").text.split("<h2>")[0].split("<h1>")[1]
    (folder / "agent-aaa.jsonl").unlink()
    with connection() as conn:
        messages.prune(conn)
        assert conn.execute("SELECT COUNT(*) FROM subagents WHERE agent_id='aaa'").fetchone()[0] == 0


# ---------------------------------------------------------------- MCP tools

def test_post_inbox_read_marks_and_channels(monkeypatch):
    first = mcp_server.message_post("deploy is blocked on review", to="*", subject="Heads-up", sender="reviewer")
    direct = mcp_server.message_post("please look at a.py", to="fixer", sender="reviewer", kind="finding")
    mcp_server.message_post("channel chatter", to="#review", sender="reviewer")
    mcp_server.message_post("not for fixer", to="someone-else", sender="reviewer")
    assert first["thread_id"] == first["id"]
    assert [m["id"] for m in mcp_server.message_inbox(unread_only=False, reader="fixer")] == [first["id"], direct["id"]]
    inbox = mcp_server.message_inbox(reader="fixer")  # re-reading history above marked nothing read
    assert [m["id"] for m in inbox] == [first["id"], direct["id"]]  # channels count only once the reader posted there
    assert mcp_server.message_inbox(reader="fixer") == []  # marked read
    mcp_server.message_post("joining", to="#review", sender="fixer")
    assert [m["body"] for m in mcp_server.message_inbox(reader="fixer")] == ["channel chatter"]
    assert [m["body"] for m in mcp_server.message_inbox(reader="reviewer")] == ["joining"]  # own messages never come back
    with connection() as conn:
        row = conn.execute("SELECT sender,sender_kind,sender_declared FROM messages WHERE id=?", (first["id"],)).fetchone()
        assert tuple(row) == ("reviewer", "subagent", 1)
        logged = [r[0] for r in conn.execute("SELECT tool FROM tool_calls ORDER BY id")]
        assert logged == ["message_post"] * 4 + ["message_inbox"] * 3 + ["message_post"] + ["message_inbox"] * 2
    monkeypatch.setattr(mcp_server, "_client_name", "claude-code")  # as recorded from the client's initialize info
    undeclared = mcp_server.message_post("from the main session")
    same_name = mcp_server.message_post("also the main session", sender="claude-code")
    monkeypatch.setattr(mcp_server, "_client_name", "codex-mcp-client")
    codex = mcp_server.message_post("from a codex worker", sender="task-17")
    with connection() as conn:
        kinds = {r[0]: tuple(r[1:]) for r in conn.execute("SELECT id,sender,sender_kind,sender_declared FROM messages")}
        assert kinds[undeclared["id"]] == ("claude-code", "claude", 0) and kinds[same_name["id"]] == ("claude-code", "claude", 0)
        assert kinds[codex["id"]] == ("task-17", "codex", 1)


def test_inbox_limit_returns_the_newest_oldest_first():
    ids = [mcp_server.message_post(f"m{i}", to="me", sender="other")["id"] for i in range(5)]
    assert [m["id"] for m in mcp_server.message_inbox(limit=2, reader="me")] == ids[3:]
    assert [m["id"] for m in mcp_server.message_inbox(limit=10, reader="me")] == ids[:3]


def test_reply_joins_thread_answers_question_and_close():
    question = mcp_server.message_post("Which branch?", kind="question", to="lead", sender="worker")
    reply = mcp_server.message_post("dev", reply_to=question["id"], sender="lead")
    assert reply["thread_id"] == question["thread_id"]
    thread = mcp_server.message_thread(question["thread_id"])
    assert [m["body"] for m in thread] == ["Which branch?", "dev"] and thread[0]["status"] == "answered" and thread[1]["reply_to"] == question["id"]
    assert mcp_server.message_close(question["thread_id"]) == {"closed": 2}
    assert {m["thread_status"] for m in mcp_server.message_thread(question["thread_id"])} == {"closed"}
    with connection() as conn:  # reopening a question that already has a reply gives answered, not open
        messages.reopen(conn, question["thread_id"])
        assert [m["status"] for m in messages.thread(conn, question["thread_id"])] == ["answered", "open"]
    with pytest.raises(ValueError, match="does not exist"): mcp_server.message_post("x", reply_to=9999)


def test_wait_returns_on_reply_and_times_out(monkeypatch):
    monkeypatch.setattr(mcp_server, "WAIT_POLL_S", 0.05)
    question = mcp_server.message_post("ok to merge?", kind="question", to="user", sender="worker")

    async def answer_soon():
        await asyncio.sleep(0.2)
        _post("yes", sender="user", sender_kind="user", reply_to=question["id"])

    async def run():
        waiting = asyncio.create_task(mcp_server.message_wait(question["thread_id"], timeout_s=10, after_id=question["id"]))
        await answer_soon()
        return await asyncio.wait_for(waiting, 5)

    result = asyncio.run(run())
    assert result["timed_out"] is False and [m["body"] for m in result["messages"]] == ["yes"] and result["last_id"] == result["messages"][0]["id"]
    timed_out = asyncio.run(mcp_server.message_wait(question["thread_id"], timeout_s=0, after_id=result["last_id"]))
    assert timed_out == {"thread_id": question["thread_id"], "messages": [], "timed_out": True, "last_id": result["last_id"]}
    with pytest.raises(ValueError): asyncio.run(mcp_server.message_wait(424242, timeout_s=0))
    with pytest.raises(ValueError): asyncio.run(mcp_server.message_wait(424242, timeout_s=0, after_id=1))
    with connection() as conn:
        assert [tuple(r) for r in conn.execute("SELECT ok,error IS NOT NULL FROM tool_calls WHERE tool='message_wait' ORDER BY id")] == [(1, 0), (1, 0), (0, 1), (0, 1)]


def test_wait_without_after_id_returns_a_reply_that_landed_earlier():
    question = mcp_server.message_post("which db?", kind="question", to="lead", sender="worker")
    _post("sqlite", sender="lead", reply_to=question["id"])  # lands before the worker starts waiting
    result = asyncio.run(mcp_server.message_wait(question["thread_id"], timeout_s=0, reader="worker"))
    assert [m["body"] for m in result["messages"]] == ["sqlite"] and not result["timed_out"]
    mcp_server.message_post("thanks, and the port?", reply_to=question["id"], sender="worker")
    again = asyncio.run(mcp_server.message_wait(question["thread_id"], timeout_s=0, reader="worker"))
    assert again["timed_out"] and again["last_id"] > result["last_id"]  # starts after the worker's own newest message
    observer = asyncio.run(mcp_server.message_wait(question["thread_id"], timeout_s=0, reader="someone-else"))
    assert [m["body"] for m in observer["messages"]] == ["sqlite", "thanks, and the port?"]  # not in the thread: after its root
    with connection() as conn:
        assert conn.execute("SELECT 1 FROM message_reads WHERE message_id=? AND reader='worker'", (result["messages"][0]["id"],)).fetchone()


def test_tool_call_logging_failure_never_masks_the_result_or_the_error(monkeypatch, caplog):
    mcp_server.message_post("warm up", sender="a")  # registers the client
    real = mcp_server.connection
    calls = {"n": 0}
    def locked_on_logging(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2: raise sqlite3.OperationalError("database is locked")  # 1: the work, 2: the bookkeeping
        return real(*args, **kwargs)
    monkeypatch.setattr(mcp_server, "connection", locked_on_logging)
    with caplog.at_level("WARNING", "devmem.mcp"):
        posted = mcp_server.message_post("committed once", sender="a")
    assert posted["id"] and "could not log" in caplog.text
    calls["n"] = 0
    with pytest.raises(ValueError, match="kind must be"): mcp_server.message_post("x", kind="nonsense", sender="a")
    with connection() as conn: assert conn.execute("SELECT COUNT(*) FROM messages WHERE body='committed once'").fetchone()[0] == 1


def test_secret_is_rejected_and_never_stored():
    for kwargs in ({"body": "token = abcdefghijklmnop"}, {"body": "fine", "subject": "key ghp_" + "a" * 30}):
        with pytest.raises(ValueError, match="secret"): mcp_server.message_post(**kwargs)
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert "abcdefghijklmnop" not in "\n".join(conn.iterdump())
    with pytest.raises(ValueError, match="8000"): mcp_server.message_post("x" * 8001)


def test_rate_limit_is_per_sender_and_ignores_subagent_events():
    with connection() as conn:  # scanner events carry agent_id and never count against the sender
        conn.executemany("INSERT INTO messages(sender,sender_kind,recipient,kind,body,status,created_at,agent_id) VALUES('chatty','subagent','#agents','event','e','open',?,'a1')",
                         [(now(),)] * 40)
    for i in range(messages.RATE_PER_MIN): mcp_server.message_post(f"m{i}", sender="chatty")
    with pytest.raises(ValueError, match="rate limit"): mcp_server.message_post("one too many", sender="chatty")
    assert mcp_server.message_post("someone else", sender="quiet")["id"]
    with connection() as conn:  # older posts fall out of the one-minute window
        conn.execute("UPDATE messages SET created_at=? WHERE sender='chatty'", ((datetime.now(UTC) - timedelta(minutes=2)).isoformat(),))
    assert mcp_server.message_post("again", sender="chatty")["id"]


def test_maintenance_prunes_closed_threads_older_than_30_days():
    old, recent, open_old, revived = _post("old"), _post("recent"), _post("still open"), _post("old root, new reply")
    with connection() as conn:
        long_ago = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        conn.execute("UPDATE messages SET created_at=? WHERE id IN (?,?,?)", (long_ago, old, open_old, revived))
        messages.mark_read(conn, [old], "user")
    reply = _post("a reply from yesterday", reply_to=revived)
    with connection() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE id=?", ((datetime.now(UTC) - timedelta(days=1)).isoformat(), reply))
        for thread_id in (old, recent, revived): messages.close(conn, thread_id)
        conn.execute("UPDATE messages SET closed_at=? WHERE thread_id IN (?,?)", (long_ago, old, revived))

    class Engine:
        name, model = "fake", None
    maintenance.run(engine=Engine(), dry_run=True)
    with connection() as conn: assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5  # a dry run changes nothing
    maintenance.run(engine=Engine())
    with connection() as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY id")] == [recent, open_old, revived, reply]
        assert conn.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 0


# ---------------------------------------------------------------- migration

def _noop(conn): pass


def _old_database(monkeypatch, path, version):
    """A database built by the real migration steps up to `version` (later steps disabled)."""
    with monkeypatch.context() as m:
        m.setattr(db, "SCHEMA_VERSION", version)
        for step in range(version + 1, SCHEMA_VERSION + 1): m.setattr(db, f"_migrate_v{step}", _noop)
        conn = connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == version
    return conn


def test_v8_database_migrates_with_backup(tmp_path, monkeypatch):
    path = tmp_path / "v8.db"; conn = _old_database(monkeypatch, path, 8)
    assert "messages" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.execute("INSERT INTO memories(kind,title,content,created_at,updated_at) VALUES('note','kept','body',?,?)", (now(), now()))
    conn.execute("INSERT INTO maintenance_runs(trigger,started_at,status) VALUES('manual',?,'success')", (now(),))
    conn.commit(); conn.close()
    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION == 12
    assert migrated.execute("SELECT title FROM memories").fetchone()[0] == "kept" and migrated.execute("SELECT COUNT(*) FROM maintenance_runs").fetchone()[0] == 1
    assert {"messages", "message_reads", "subagents", "subagent_tools", "subagent_texts"} <= {r[0] for r in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    backup = sqlite3.connect(next((tmp_path / "backups").glob("devmem-before-v12-from-v8-*.db")))
    assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == 8 and backup.execute("SELECT title FROM memories").fetchone()[0] == "kept"
    assert not list((tmp_path / "backups").glob("*.tmp"))


def test_v9_database_migrates_to_v10_and_repairs_open_agent_threads(tmp_path, monkeypatch):
    lines = [_line("user", "Review it.", 300, "old"),
             _line("assistant", [{"type": "text", "text": "Looking."}], 299, "old", stop="tool_use"),
             _line("assistant", [{"type": "tool_use", "id": "h", "name": "SubagentHandback", "input": {"message": "All good."}}], 298, "old", stop="tool_use"),
             _line("user", [{"type": "tool_result", "tool_use_id": "h", "content": "ok"}], 298, "old", toolEndsTurn=True)]
    folder = _subagent(_claude_home(), "old", lines, _meta("old"))  # the sidecar never says finished, as for most real agents
    path = Path(os.environ["DEVMEM_DB"]); conn = _old_database(monkeypatch, path, 9)
    size = (folder / "agent-old.jsonl").stat().st_size
    # What the v9 scanner left behind: everything read, an open spawn thread, no report.
    conn.execute("""INSERT INTO subagents(agent_id,session_id,project,agent_type,started_at,last_activity_at,task,last_text,source_file,byte_offset,meta_mtime)
        VALUES('old',?,'/work/demo','general-purpose',?,?,'Review it.','All good.',?,?,?)""",
                 (SESSION, _ts(300), _ts(298), str(folder / "agent-old.jsonl"), size, (folder / "agent-old.meta.json").stat().st_mtime))
    spawn = conn.execute("""INSERT INTO messages(project,sender,sender_kind,recipient,kind,subject,body,status,created_at,agent_id)
        VALUES('/work/demo','general-purpose','subagent','#agents','event','started: Task old','Review it.','open',?,'old')""", (_ts(300),)).lastrowid
    conn.execute("UPDATE messages SET thread_id=id WHERE id=?", (spawn,)); conn.execute("UPDATE subagents SET spawn_msg_id=? WHERE agent_id='old'", (spawn,))
    conn.commit(); conn.close()
    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 12
    assert migrated.execute("SELECT byte_offset FROM subagents").fetchone()[0] == 0  # re-read once for remarks and the end marker
    assert list((tmp_path / "backups").glob("devmem-before-v12-from-v9-*.db"))
    migrated.close()
    ingest_all(); ingest_all()
    with connection() as conn:
        reports = conn.execute("SELECT body,status FROM messages WHERE agent_id='old' AND id!=?", (spawn,)).fetchall()  # the v9 spawn row keeps its old subject
        assert [tuple(r) for r in reports] == [("All good.", "closed")]  # posted once
        assert conn.execute("SELECT status FROM messages WHERE id=?", (spawn,)).fetchone()[0] == "closed"
        assert [t["body"] for t in messages.subagent(conn, "old")["texts"]] == ["Looking."]


def test_v10_migration_rolls_back_on_error(tmp_path, monkeypatch):
    path = tmp_path / "v9.db"; _old_database(monkeypatch, path, 9).close()
    monkeypatch.setattr(db, "_V10_STATEMENTS", db._V10_STATEMENTS + ("THIS IS NOT SQL",))
    with pytest.raises(sqlite3.OperationalError): connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 9
    assert "last_terminal" not in {r[1] for r in raw.execute("PRAGMA table_info(subagents)")}


def test_failed_backup_leaves_no_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "v9.db"; _old_database(monkeypatch, path, 9).close()
    real_connect = sqlite3.connect
    class Boom:
        def __init__(self, conn): self.conn = conn
        def backup(self, target): target.execute("CREATE TABLE half(x)"); raise sqlite3.OperationalError("disk full")
        def close(self): self.conn.close()
    def connect_patch(target, *a, **k):
        conn = real_connect(target, *a, **k)
        return Boom(conn) if str(target) == str(path) and not a and not k else conn
    monkeypatch.setattr(db.sqlite3, "connect", connect_patch)
    with pytest.raises(RuntimeError, match="backup failed"): db._backup_before_migration(real_connect(path), 10, 9)
    assert list((tmp_path / "backups").iterdir()) == []


# ---------------------------------------------------------------- panel

def _client():
    from devmem.panel import app as panel
    panel._cache.value = None
    return TestClient(panel.app, base_url="http://127.0.0.1")  # not a context manager: no background ingest


def test_main_tab_has_messages_not_tokens_and_overview_has_tokens():
    client = _client()
    html = client.get("/").text
    assert 'class="panel panel-limits"' in html and 'id="msgs"' in html and ">Messages</a>" in html
    for gone in ('id="tok-today"', 'id="tok-week"', 'id="spark"', 'id="models"', "Models today"):
        assert gone not in html
    overview = client.get("/overview").text
    for moved in ('id="tok-today"', 'id="tok-week"', 'id="spark"', 'id="models"', "Models today", "devmem.renderTokens("):
        assert moved in overview
    js = (Path(__file__).parent.parent / "devmem" / "panel" / "static" / "brain.js").read_text()
    assert '"messages"' in js and "renderTokens" not in js and "/messages?chat=" in js


def test_cross_site_writes_are_refused():
    client = _client()
    question = _post("Ship it?", kind="question", to="user", sender="worker")
    evil = {"Origin": "https://evil.example"}
    for path, data in ((f"/messages/thread/{question}/reply", {"body": "yes"}), (f"/messages/thread/{question}/close", None),
                       ("/messages/chat/send", {"key": f"t:{question}", "body": "yes"}), ("/memory", {"content": "x"}), ("/maintenance/1/revert", None)):
        assert client.post(path, data=data, headers=evil, follow_redirects=False).status_code == 403
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"Origin": "null"}, {"Origin": "http://127.0.0.1:9999"}):
        assert client.post(f"/messages/thread/{question}/close", headers=headers, follow_redirects=False).status_code == 403
    with connection() as conn: assert len(messages.thread(conn, question)) == 1 and messages.thread(conn, question)[0]["status"] == "open"
    own = {"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}
    assert client.post(f"/messages/thread/{question}/reply", data={"body": "yes"}, headers=own, follow_redirects=False).status_code == 303
    assert client.get("/messages", headers=evil).status_code == 200  # reads are not affected


def test_messages_page_chat_list_filters_and_thread_routes():
    client = _client()
    question = _post("Ship it?", kind="question", to="user", sender="worker", project="/work/demo")
    finding = _post("a.py:3 swallows errors", kind="finding", to="#review", sender="hunter", project="/work/other", declared=True)
    to_user = _post("FYI done", to="user", sender="worker", project="/work/demo")
    with connection() as conn:
        assert messages.badge(conn) == 2 and messages.unread_count(conn) == 3  # the open question to the user counts once
    page = client.get("/messages").text
    assert "Ship it?" in page and "a.py:3 swallows errors" in page and 'id="nav-msg-badge"' in page and 'id="chat-list"' in page
    assert "#review" in page  # the channel is its own chat
    by_kind = client.get("/messages", params={"kind": "finding"}).text
    assert "a.py:3" in by_kind and "Ship it?" not in by_kind
    by_question = client.get("/messages", params={"question": "1"}).text
    assert "Ship it?" in by_question and "a.py:3" not in by_question
    by_project = client.get("/messages", params={"project": "/work/other"}).text
    assert "a.py:3" in by_project and "Ship it?" not in by_project
    by_agent = client.get("/messages", params={"agent": "hunter"}).text
    assert "a.py:3" in by_agent and "Ship it?" not in by_agent
    with connection() as conn: messages.close(conn, to_user)
    closed = client.get("/messages", params={"status": "closed"}).text
    assert "FYI done" in closed and "Ship it?" not in closed
    with connection() as conn: messages.reopen(conn, to_user)
    assert [c["key"] for c in client.get("/api/chats", params={"status": "open"}).json()] == [f"t:{to_user}", "c:#review", f"t:{question}"]
    thread = client.get(f"/messages/thread/{question}").text
    assert "Ship it?" in thread and "Send reply" in thread and "Close thread" in thread and f"chat=t%3A{question}" in thread
    with connection() as conn:  # viewing marks the question read, but both threads are still open and addressed to the user
        assert messages.badge(conn) == 2 and messages.unread_count(conn) == 2
    assert client.post(f"/messages/thread/{question}/reply", data={"body": "yes, ship"}, follow_redirects=False).status_code == 303
    assert client.post(f"/messages/thread/{question}/reply", data={"body": "password = hunter2hunter2"}, follow_redirects=False).status_code == 400
    with connection() as conn:
        rows = messages.thread(conn, question)
        assert [(r["sender"], r["sender_kind"], r["recipient"], r["body"]) for r in rows][1] == ("user", "user", "worker", "yes, ship")
        assert rows[0]["status"] == "answered" and len(rows) == 2 and messages.badge(conn) == 1
    # no open question left: a reply continues after the newest message, addressed to its sender
    _post("one more thing", sender="worker", reply_to=question)
    client.post(f"/messages/thread/{question}/reply", data={"body": "noted"})
    with connection() as conn:
        last = messages.thread(conn, question)[-1]
        assert (last["body"], last["recipient"], last["reply_to"]) == ("noted", "worker", messages.thread(conn, question)[-2]["id"])
    client.post(f"/messages/thread/{to_user}/close")
    with connection() as conn: assert messages.badge(conn) == 0
    assert "Reopen" in client.get(f"/messages/thread/{to_user}").text
    client.post(f"/messages/thread/{to_user}/reopen")
    with connection() as conn: assert messages.thread(conn, to_user)[0]["status"] == "open"
    assert client.get("/messages/thread/99999").status_code == 404
    assert client.get("/messages", params={"chat": "t:99999"}).status_code == 404
    api = client.get("/api/messages").json()
    assert {"items", "unread", "badge", "sig"} <= set(api) and api["items"][0]["chat"].startswith(("t:", "c:"))
    assert finding


def test_badge_ignores_questions_in_closed_threads():
    question = _post("Which way?", kind="question", to="lead", sender="worker")
    with connection() as conn:
        assert messages.badge(conn) == 1
        messages.close(conn, question)
        assert messages.badge(conn) == 0


def test_session_chat_shows_tasks_remarks_tool_groups_reports_and_user_replies():
    _two_agents(_claude_home())
    ingest_all()
    client = _client()
    key = "s:" + SESSION
    assert [c["key"] for c in client.get("/api/chats").json()] == [key]
    chat = client.get("/api/chat", params={"key": key}).json()
    members = chat["members"]
    assert members["parent"]["name"] == "Claude" and members["agent:aaa"]["name"] == "code reviewer" and members["agent:bbb"]["status"] == "running"
    bubbles = [(i["who"], i.get("label"), i.get("to"), i["text"]) for i in chat["items"] if i["type"] == "bubble"]
    assert ("parent", "task", "@code reviewer", "Review the diff in /work/demo and report findings. Use token = [redacted]") in bubbles
    assert ("agent:aaa", None, None, "Reading a.py next.") in bubbles
    assert ("agent:aaa", "report", None, "Found 2 issues: a.py:3 and a.py:9. Key [redacted]") in bubbles
    assert len([b for b in bubbles if b[3].startswith("Found 2 issues")]) == 1  # the closing remark is not repeated next to the report
    groups = [i for i in chat["items"] if i["type"] == "tools"]
    assert [(g["who"], [t["name"] for t in g["tools"]]) for g in groups] == [("agent:aaa", ["Bash"]), ("agent:aaa", ["Read"]), ("agent:bbb", ["Grep"])]
    order = [i["key"] for i in chat["items"]]
    aaa_task = next(i["key"] for i in chat["items"] if i.get("label") == "task" and i["agent_id"] == "aaa")
    aaa_report = next(i["key"] for i in chat["items"] if i.get("label") == "report")
    assert order.index(aaa_task) < order.index(groups[0]["key"]) < order.index(aaa_report)
    assert chat["target"]["name"] == "code reviewer"  # bbb has not said anything yet; aaa spoke last
    page = client.get("/messages", params={"chat": key}).text
    assert 'id="chat-data"' in page and "code-reviewer" in page and 'id="chat-form"' in page
    assert client.post("/messages/chat/send", data={"key": key, "body": "Thanks, fix a.py:3 first"}, headers={"Accept": "application/json"}).json()["id"]
    chat = client.get("/api/chat", params={"key": key}).json()
    mine = [i for i in chat["items"] if i["mine"]]
    assert [(i["text"], i["who"]) for i in mine] == [("Thanks, fix a.py:3 first", "user")]
    with connection() as conn:
        sent = conn.execute("SELECT recipient,reply_to FROM messages WHERE sender_kind='user'").fetchone()
        assert sent["recipient"] == "pr-review-toolkit:code-reviewer" and sent["reply_to"] == messages.subagent(conn, "aaa")["report_msg_id"]
    assert client.get("/messages/agent/aaa").status_code == 200 and "Reading a.py next." in client.get("/messages/agent/aaa").text
    assert client.get("/messages/agent/nope").status_code == 404
    agents = client.get("/api/state").json()["agents"]["subagents"][SESSION]
    assert [(a["agent_id"], a["status"], a["last_tool"]) for a in agents] == [("bbb", "running", "Grep"), ("aaa", "finished", "Read")]


def test_chat_send_answers_the_open_question_and_message_post_threads_join_the_session_chat():
    _two_agents(_claude_home())
    ingest_all()  # usage from the session's logs: the session ran in /work/demo around now
    question = _post("Merge now or after review?", kind="question", to="user", sender="claude-code", project="/work/demo")
    lone = _post("codex worker notes", sender="task-9", sender_kind="codex", project="/work/elsewhere")
    client = _client()
    key = "s:" + SESSION
    keys = [c["key"] for c in client.get("/api/chats").json()]
    assert key in keys and f"t:{lone}" in keys and f"t:{question}" not in keys
    chat = client.get("/api/chat", params={"key": key}).json()
    assert ("parent", "question") in [(i["who"], i.get("label")) for i in chat["items"]]
    assert chat["target"] == {"reply_to": question, "to": "claude-code", "name": "claude-code", "why": "open question"}
    assert client.post("/messages/chat/send", data={"key": key, "body": "after review"}, follow_redirects=False).status_code == 303
    with connection() as conn:
        assert messages.thread(conn, question)[0]["status"] == "answered" and messages.thread(conn, question)[-1]["body"] == "after review"
    assert client.post("/messages/chat/send", data={"key": key, "body": "token = abcdefghijklmnop"}).status_code == 400
    assert client.post("/messages/chat/send", data={"key": "t:4242", "body": "x"}).status_code == 404
    feed = client.get("/api/messages").json()["items"]
    assert {i["chat"] for i in feed} == {key, f"t:{lone}"} and feed[0]["mine"] and feed[0]["who"] == "You"


def test_sse_sends_messages_event_after_a_post_and_survives_a_messages_failure(monkeypatch):
    from starlette.requests import Request
    from devmem.panel import app as panel
    monkeypatch.setattr(panel, "STREAM_EVERY_S", 0.01)
    panel._cache.value = None; panel._shutdown_event.clear()

    def stream(only=""):
        scope = {"type": "http", "method": "GET", "path": "/api/stream", "raw_path": b"/api/stream", "query_string": b"", "headers": [],
                 "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8765), "scheme": "http"}
        async def receive():  # never disconnects; is_disconnected() cancels this wait immediately
            await asyncio.sleep(3600)
        return panel.api_stream(Request(scope, receive), only=only)

    async def drive():
        iterator = (await stream()).body_iterator
        first = await anext(iterator)
        assert "event: state" in first and "event: messages" in first
        _post("new finding", kind="finding", sender="hunter", sender_kind="subagent", declared=True)
        for _ in range(20):
            chunk = await asyncio.wait_for(anext(iterator), 2)
            if "event: messages" in chunk:
                payload = json.loads(chunk.split("event: messages\ndata: ", 1)[1].split("\n", 1)[0])
                assert payload["items"][0]["headline"] == "hunter: new finding" and payload["unread"] == 1 and payload["items"][0]["chat"]
                break
        else: raise AssertionError("no messages event after a post")
        only = (await stream("messages")).body_iterator
        assert "event: state" not in await anext(only)
        def broken(): raise sqlite3.OperationalError("boom")
        monkeypatch.setattr(panel, "_messages", broken)
        states = 0
        for _ in range(6):
            states += "event: state" in await asyncio.wait_for(anext(iterator), 2)
        assert states >= 3  # state keeps flowing while messages fail

    try: asyncio.run(drive())
    finally: panel._shutdown_event.set(); panel._shutdown_event.clear()


def test_maintenance_page_marks_corrupt_evidence():
    with connection() as conn:
        run = conn.execute("INSERT INTO maintenance_runs(trigger,started_at,status) VALUES('manual',?,'success')", (now(),)).lastrowid
        conn.execute("""INSERT INTO maintenance_changes(run_id,memory_id,action,applied,summary,evidence,created_at)
            VALUES(?,1,'flag',0,'look at this','{not json',?)""", (run, now()))
    assert "corrupt evidence record" in _client().get("/maintenance").text


@pytest.mark.parametrize(("report", "outcome", "findings"), [
    ("Found 15 issues (1 critical, 3 high).", "15 issues found (1 critical, 3 high)", True),
    ("## Critical\n- a\n- b\n\n## High\n- c", "3 issues found", True),
    ("LGTM — all pass.", "no issues found", False),
    ("**Implemented** the small cleanup. More detail follows.", "Implemented the small cleanup.", False),
])
def test_report_headlines_are_short_plain_and_deterministic(report, outcome, findings):
    assert messages.report_outcome(report) == (outcome, findings)
    assert len(messages.report_outcome("x" * 300)[0]) <= 70


def test_headline_names_subject_fallback_and_running_line():
    assert messages.friendly_name("pr-review-toolkit:silent-failure-hunter") == "bug hunter"
    assert messages.friendly_name("Explore") == "explorer"
    mid = _post("Implemented the parser and added tests.", sender="general-purpose", sender_kind="subagent")
    with connection() as conn:
        row = dict(conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())
        assert row["subject"] == "worker: Implemented the parser and added tests."
        assert messages.headline_for(row) == ("worker: Implemented the parser and added tests.", "✉")
    _two_agents(_claude_home()); ingest_all()
    with connection() as conn:
        chat = messages.chat(conn, "s:" + SESSION)
        live = [item for item in chat["headlines"] if item["key"] == "live:bbb"]
        assert len(live) == 1 and live[0]["headline"].startswith("bug hunter: working — Grep")
        spawn = next(item for item in chat["headlines"] if item["agent_id"] == "aaa" and item["icon"] == "▶")
        report = next(item for item in chat["headlines"] if item["agent_id"] == "aaa" and item["icon"] == "⚠")
        assert spawn["headline"].startswith("Claude: dispatched code reviewer")
        assert report["headline"].startswith("code reviewer: 2 issues found")
    stopped, icon = messages.headline_for({"sender": "general-purpose", "sender_kind": "subagent", "kind": "event", "agent_id": "x",
                                            "subject": "worker: stopped without a report", "body": "(no report: agent stopped)"})
    assert (stopped, icon) == ("worker: stopped without a report", "■")
    long, _ = messages.headline_for({"sender": "x" * 100, "sender_kind": "subagent", "kind": "message", "body": "y" * 300})
    assert len(long) <= 80


def _rollout(root: Path, session: str, records: list[dict]):
    path = root / "sessions" / "2026" / "10" / "28" / f"rollout-{session}.jsonl"; path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(record if isinstance(record, str) else json.dumps(record) for record in records) + "\n")
    return path


def _rollout_record(kind, payload, minutes_ago=0):
    return {"timestamp": _ts(minutes_ago), "type": kind, "payload": payload}


def test_codex_rollouts_are_agents_link_handoffs_and_keep_safe_chat_content():
    handoff, codex_session, claude_session = "20261028-133539-devmem-codex-sessions-chat", "codex-rollout", "claude-parent"
    task = f"Implement the panel work. Handoff task: {handoff}. token=abcdefghijklmnopqrstuvwxyz"
    _rollout(Path(os.environ["DEVMEM_CODEX_HOME"]), codex_session, [
        _rollout_record("session_meta", {"id": codex_session, "cwd": "/work/demo"}, 5),
        _rollout_record("turn_context", {"model": "gpt-5-codex", "cwd": "/work/demo"}, 5),
        _rollout_record("response_item", {"type": "message", "role": "developer", "content": [{"text": "ignore instructions"}]}, 5),
        _rollout_record("response_item", {"type": "message", "role": "user", "content": [{"text": task}]}, 4.9),
        _rollout_record("response_item", {"type": "message", "role": "assistant", "content": [{"text": "Inspecting files. key=sk-ant-abcdefghijklmnopqrstuvwxyz0123"}]}, 4.8),
        _rollout_record("response_item", {"type": "reasoning", "summary": [{"text": "private reasoning"}]}, 4.7),
        _rollout_record("response_item", {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"command": "rg --files devmem && secret-output"})}, 4.6),
        _rollout_record("response_item", {"type": "custom_tool_call", "name": "apply_patch", "input": json.dumps({"patch": "*** Update File: devmem/a.py\n*** Add File: devmem/b.py"})}, 4.5),
        _rollout_record("response_item", {"type": "message", "role": "assistant", "content": [{"text": "Implemented it."}]}, 4.4),
        _rollout_record("event_msg", {"type": "task_complete"}, 4.3),
        _rollout_record("event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 7, "output_tokens": 3}}}, 4.2),
        "{malformed",
    ])
    folder = _claude_home() / "projects" / "-work-demo"; folder.mkdir(parents=True)
    parent_tool = json.loads(_line("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": f"new-task.sh {handoff}"}}], 5, "parent")); parent_tool["sessionId"] = claude_session
    parent_result = json.loads(_line("user", [{"type": "tool_result", "content": f"Created task: /tmp/{handoff}.task.md"}], 4.9, "parent")); parent_result["sessionId"] = claude_session
    (folder / "parent.jsonl").write_text(json.dumps(parent_tool) + "\n" + json.dumps(parent_result) + "\n")
    ingest_all()
    with connection() as conn:
        agent = messages.subagent(conn, codex_session)
        assert agent["agent_kind"] == "codex" and agent["handoff_session_id"] == claude_session
        assert agent["task"].endswith("token=[redacted]") and agent["report"] == "Implemented it." and agent["tokens"] == 10
        assert [t["arg"] for t in agent["tools"]] == ["rg --files devmem && secret-output", "devmem/a.py, devmem/b.py"]
        assert [t["body"] for t in agent["texts"]] == ["Inspecting files. key=[redacted]", "Implemented it."]
        assert "private reasoning" not in "\n".join(conn.iterdump())
        joined = messages.chat(conn, "s:" + claude_session)
        assert any(item["headline"].startswith("Claude: dispatched Codex") for item in joined["headlines"])
        assert any(item["headline"].startswith("Codex (gpt-5-codex): Implemented") for item in joined["headlines"])


def test_interactive_codex_chat_and_silent_stop_after_cutoff():
    session = "codex-interactive"
    _rollout(Path(os.environ["DEVMEM_CODEX_HOME"]), session, [
        _rollout_record("session_meta", {"id": session, "cwd": "/work/demo"}, 200),
        _rollout_record("response_item", {"type": "message", "role": "user", "content": [{"text": "Inspect the project."}]}, 200),
    ])
    ingest_all()
    with connection() as conn:
        agent = messages.subagent(conn, session)
        assert agent["status"] == "stopped" and messages.chat(conn, "d:" + session)["title"].startswith("Codex · demo")
        assert any(item["icon"] == "■" for item in messages.chat(conn, "d:" + session)["headlines"])


def test_devmem_spawned_rollout_is_not_an_agent_timeline():
    session = "devmem-child"
    _rollout(Path(os.environ["DEVMEM_CODEX_HOME"]), session, [
        _rollout_record("session_meta", {"id": session, "cwd": "/work/demo"}, 1),
        _rollout_record("response_item", {"type": "message", "role": "user", "content": [{"text": "<devmem-spawned>\nDo local work."}]}, 1),
        _rollout_record("response_item", {"type": "message", "role": "assistant", "content": [{"text": "Done."}]}, 1),
        _rollout_record("event_msg", {"type": "task_complete"}, 1),
    ])
    ingest_all()
    with connection() as conn:
        assert conn.execute("SELECT 1 FROM subagents WHERE agent_id=?", (session,)).fetchone() is None


def test_marker_quoted_later_in_a_rollout_keeps_the_agent_timeline():
    session = "quotes-marker"
    _rollout(Path(os.environ["DEVMEM_CODEX_HOME"]), session, [
        _rollout_record("session_meta", {"id": session, "cwd": "/work/demo"}, 1),
        _rollout_record("response_item", {"type": "message", "role": "user", "content": [{"text": "Review the ingest diff."}]}, 1),
        _rollout_record("response_item", {"type": "message", "role": "user", "content": [{"text": "The diff adds <devmem-spawned>\nto prompts."}]}, 2),
        _rollout_record("response_item", {"type": "message", "role": "assistant", "content": [{"text": "Looks fine."}]}, 3),
        _rollout_record("event_msg", {"type": "task_complete"}, 3),
    ])
    ingest_all()
    with connection() as conn:
        row = conn.execute("SELECT finished,last_text FROM subagents WHERE agent_id=?", (session,)).fetchone()
        context = conn.execute("SELECT context FROM ingest_offsets WHERE source_file LIKE ?", (f"%{session}%",)).fetchone()[0]
    assert row["finished"] == 1 and row["last_text"] == "Looks fine." and "devmem_spawned" not in context


def test_derived_subject_is_redacted_instead_of_rejected():
    with connection() as conn:
        message_id = messages.post(conn, "Fixed the `token`: `refresh_token_handler` path.", sender="worker", sender_kind="claude")
        subject = conn.execute("SELECT subject FROM messages WHERE id=?", (message_id,)).fetchone()[0]
    assert "[redacted]" in subject


def test_spawned_claude_session_does_not_make_chat_attachment_ambiguous():
    with connection() as conn:
        root = messages.post(conn, "Question for the current agent.", sender="worker", sender_kind="claude", project="/work/demo")
        created = conn.execute("SELECT * FROM messages WHERE id=?", (root,)).fetchone()
        for session, source in (("normal", "normal.jsonl"), ("spawned", "spawned.jsonl")):
            conn.execute("""INSERT INTO usage_events(agent,session_id,project,model,ts,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,source_file,source_offset)
                VALUES('claude',?,'/work/demo','claude',?,0,0,0,0,0,?,0)""", (session, created["created_at"], source))
        conn.execute("INSERT INTO ingest_offsets(source_file,byte_offset,context) VALUES('spawned.jsonl',1,'{\"devmem_spawned\": true}')")
        assert messages.chat_keys(conn, [dict(created)])[root] == "s:normal"


def test_v10_database_migrates_to_v11_with_codex_columns(tmp_path, monkeypatch):
    path = tmp_path / "v10.db"; _old_database(monkeypatch, path, 10).close()
    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 12
    assert {"agent_kind", "model", "handoff_id", "handoff_session_id"} <= {row[1] for row in migrated.execute("PRAGMA table_info(subagents)")}
    assert list((tmp_path / "backups").glob("devmem-before-v12-from-v10-*.db"))


def test_chat_title_and_state_come_as_parts_and_keep_a_dot_inside_a_subject():
    client = _client()
    _post("body", kind="question", to="user", sender="worker", project="/work/demo", subject="Ship it · now?")
    with connection() as conn:
        row = messages.chats(conn)[0]
        assert row["title_parts"] == ["Ship it · now?"] and row["title"] == "Ship it · now?"
        assert row["state_parts"] == ["1 thread", "1 question for you"] and row["state"] == " · ".join(row["state_parts"])
        assert messages.chat(conn, row["key"])["title_parts"] == ["Ship it · now?"]
    page = client.get("/messages", params={"chat": row["key"]}).text
    assert '<span class="part">Ship it · now?</span>' in page
