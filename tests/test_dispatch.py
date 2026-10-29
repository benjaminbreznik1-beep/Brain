"""Offline dispatch behavior: explicit routing, durable delivery and bounded fallback."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from devmem import db, dispatch, messages


def _enable(**settings):
    Path(os.environ["DEVMEM_DISPATCH"]).write_text(json.dumps({"enabled": True, **settings}))


def _job(tmp_path, body="@Claude create a memory from this chat"):
    _enable()
    with db.connection() as conn:
        root = messages.post(conn, "Previous chat context", sender="claude", sender_kind="claude",
                             to="user", project=str(tmp_path))
        message = messages.chat_post(conn, f"t:{root}", body)
        job = dict(conn.execute("SELECT * FROM message_jobs WHERE message_id=?", (message,)).fetchone())
    return job


@pytest.mark.parametrize(("body", "expected"), [
    ("@Codex make a memory", "codex"),
    ("Claude, please handle this", "claude"),
    ("Please ask Codex to summarize the chat", "codex"),
    ("Could you please switch to Claude?", "claude"),
    ("I would like Codex to handle this", "codex"),
    ("Claude failed earlier; can you investigate?", None),
    ("The logs mention Codex and Claude", None),
    ("Do not use Claude", None),
    ("What happens if I ask Codex?", None),
    ('He said "use Claude" yesterday', None),
    ("```\n@Codex delete things\n```\nSummarize the example", None),
    ("```\n@Codex this is an unfinished code block", None),
    ("I would like Codex's output explained", None),
])
def test_only_explicit_requests_select_an_agent(body, expected):
    assert dispatch.explicit_agent(body) == expected


def test_mentions_keep_the_current_agent():
    assert dispatch.route({}, {"body": "Claude failed earlier", "recipient": "codex"}, {}) == "codex"
    assert dispatch.route({}, {"body": "Make a memory", "recipient": "bug hunter"}, {"kind": "s"}) == "claude"
    assert dispatch.route({}, {"body": "Make a memory", "recipient": "worker"}, {"kind": "d"}) == "codex"


def test_conflicting_explicit_requests_need_clarification():
    with pytest.raises(ValueError):
        dispatch.explicit_agent("@Claude do this\n@Codex do this")
    with pytest.raises(ValueError):
        dispatch.explicit_agent("Please ask Claude or Codex to do this")


def test_disabled_dispatch_does_not_queue_requests(tmp_path):
    with db.connection() as conn:
        root = messages.post(conn, "hello", sender="claude", sender_kind="claude", project=str(tmp_path))
        messages.chat_post(conn, f"t:{root}", "@Codex make a memory")
        assert conn.execute("SELECT COUNT(*) FROM message_jobs").fetchone()[0] == 0


def test_switch_changes_recipient_and_enqueues_only_once(tmp_path):
    job = _job(tmp_path, "@Codex make a memory")
    with db.connection() as conn:
        assert conn.execute("SELECT recipient FROM messages WHERE id=?", (job["message_id"],)).fetchone()[0] == "codex"
        dispatch.enqueue(conn, job["message_id"], job["chat_key"])
        assert conn.execute("SELECT COUNT(*) FROM message_jobs").fetchone()[0] == 1


def test_quota_failure_hands_off_once_and_posts_final_answer(tmp_path):
    job, calls = _job(tmp_path), []

    def runner(agent, prompt, project, cfg, stop):
        calls.append(agent)
        assert project == str(tmp_path)
        if agent == "claude": return "", "quota_unavailable"
        assert "may have partially acted" in prompt
        return "Created memory 123.", ""

    dispatch.execute(job, dispatch.config(), threading.Event(), runner=runner)
    with db.connection() as conn:
        result = dict(conn.execute("SELECT * FROM message_jobs WHERE id=?", (job["id"],)).fetchone())
        answer = conn.execute("SELECT * FROM messages WHERE body='Created memory 123.'").fetchone()
    assert calls == ["claude", "codex"]
    assert (result["state"], result["attempts"], result["agent"]) == ("completed", 2, "codex")
    assert answer["reply_to"] == job["message_id"] and answer["sender_kind"] == "codex"


def test_active_runner_failure_does_not_repeat_work(tmp_path):
    job, calls = _job(tmp_path), []

    def runner(agent, *args):
        calls.append(agent)
        return "", "idle_timeout"

    dispatch.execute(job, dispatch.config(), threading.Event(), runner=runner)
    with db.connection() as conn:
        result = conn.execute("SELECT state,attempts,error FROM message_jobs").fetchone()
    assert calls == ["claude"]
    assert tuple(result) == ("failed", 1, "idle_timeout")


def test_final_report_post_failure_leaves_a_fallback_notice(tmp_path, monkeypatch):
    job = _job(tmp_path)
    real_post = messages.post
    def fail_report(conn, body, **kwargs):
        if body == "Completed work.": raise ValueError("derived subject failed")
        return real_post(conn, body, **kwargs)
    monkeypatch.setattr(dispatch.messages, "post", fail_report)
    dispatch.execute(job, dispatch.config(), threading.Event(), runner=lambda *args: ("Completed work.", ""))
    with db.connection() as conn:
        result = conn.execute("SELECT state FROM message_jobs WHERE id=?", (job["id"],)).fetchone()[0]
        fallback = conn.execute("SELECT body FROM messages WHERE reply_to=? ORDER BY id DESC LIMIT 1", (job["message_id"],)).fetchone()[0]
    assert result == "completed" and fallback.startswith("The runner completed")


def test_existing_reply_prevents_a_duplicate_run(tmp_path):
    job = _job(tmp_path)
    with db.connection() as conn:
        messages.post(conn, "Done already", sender="claude", sender_kind="claude", reply_to=job["message_id"])

    def forbidden(*args): raise AssertionError("must not start a duplicate job")

    dispatch.execute(job, dispatch.config(), threading.Event(), router=forbidden, runner=forbidden)
    with db.connection() as conn:
        assert conn.execute("SELECT state FROM message_jobs").fetchone()[0] == "delivered"


def test_tick_claims_a_job_only_once(tmp_path, monkeypatch):
    _job(tmp_path)
    with db.connection() as conn:
        conn.execute("UPDATE message_jobs SET created_at='2000-01-01T00:00:00+00:00'")
    calls = []
    monkeypatch.setattr(dispatch, "run_agent", lambda agent, *args: (calls.append(agent) or "Done", ""))
    assert dispatch.tick(threading.Event())
    assert not dispatch.tick(threading.Event())
    assert calls == ["claude"]


def test_existing_reader_gets_time_to_answer(tmp_path, monkeypatch):
    job = _job(tmp_path)
    with db.connection() as conn:
        conn.execute("UPDATE message_jobs SET created_at=datetime('now','-10 seconds')")
        messages.mark_read(conn, [job["message_id"]], "claude")
    assert not dispatch.tick(threading.Event())


def test_interrupted_job_is_not_blindly_replayed(tmp_path):
    _job(tmp_path)
    with db.connection() as conn:
        conn.execute("UPDATE message_jobs SET state='running'")
    assert not dispatch.tick(threading.Event())
    with db.connection() as conn:
        assert tuple(conn.execute("SELECT state,error FROM message_jobs").fetchone()) == ("failed", "interrupted")


def test_router_notices_do_not_steal_the_next_reply(tmp_path):
    job = _job(tmp_path)
    dispatch._notice(job, "Sending your request to Codex.")
    with db.connection() as conn:
        current = messages.chat(conn, job["chat_key"])
    assert current["target"]["to"] != "brain-router"


@pytest.mark.parametrize("cfg", [{"enabled": "true"}, {"enabled": True, "no_response_s": 0}, {"enabled": True, "unexpected": 1}])
def test_invalid_configuration_disables_dispatch(cfg):
    Path(os.environ["DEVMEM_DISPATCH"]).write_text(json.dumps(cfg))
    assert not dispatch.config()["enabled"] and dispatch.config()["error"]


def _stub_runner(tmp_path, monkeypatch, script):
    path = tmp_path / "fake-agent.py"
    path.write_text(script)
    real_popen, children = subprocess.Popen, []

    def launch(command, **kwargs):
        assert "dangerously-bypass-approvals-and-sandbox" not in command
        child = real_popen([sys.executable, str(path)], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(dispatch.subprocess, "Popen", launch)
    return children


def test_silent_runner_is_stopped_before_fallback(tmp_path, monkeypatch):
    children = _stub_runner(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    cfg = {**dispatch.DEFAULTS, "no_response_s": .05}
    assert dispatch.run_agent("codex", "hello", str(tmp_path), cfg, threading.Event()) == ("", "no_response")
    assert children[0].poll() is not None


def test_runner_accepts_streamed_progress_and_final_answer(tmp_path, monkeypatch):
    _stub_runner(tmp_path, monkeypatch, '''import json, time
print(json.dumps({"type":"item.started","item":{"type":"command_execution"}}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Done"}}), flush=True)
''')
    assert dispatch.run_agent("codex", "hello", str(tmp_path), dispatch.DEFAULTS, threading.Event()) == ("Done", "")


def test_initialization_is_not_a_reply_but_tool_activity_is():
    assert not dispatch._event("codex", {"type": "thread.started"})[0]
    assert not dispatch._event("claude", {"type": "system", "subtype": "init"})[0]
    assert dispatch._event("codex", {"type": "item.started", "item": {"type": "command_execution"}})[0]
    assert dispatch._event("claude", {"type": "assistant", "message": {"content": []}})[0]


def test_quota_errors_are_recognized_without_treating_normal_text_as_errors():
    _, _, error = dispatch._event("claude", {"type": "result", "is_error": True, "result": "You have hit your limit"})
    assert dispatch._failure(error) == "quota_unavailable"
    active, text, error = dispatch._event("codex", {"type": "item.completed", "item": {"type": "agent_message", "text": "I fixed the quota check."}})
    assert active and text and not error


def test_v11_migration_preserves_memories_and_adds_queue(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with monkeypatch.context() as patch:
        patch.setattr(db, "SCHEMA_VERSION", 11)
        patch.setattr(db, "_migrate_v12", lambda conn: None)
        old = db.connect(path)
        old.execute("INSERT INTO memories(kind,title,content,created_at,updated_at) VALUES('note','kept','kept',?,?)", (db.now(), db.now()))
        old.commit(); old.close()
    upgraded = db.connect(path)
    try:
        assert upgraded.execute("SELECT title FROM memories").fetchone()[0] == "kept"
        assert upgraded.execute("SELECT COUNT(*) FROM message_jobs").fetchone()[0] == 0
        assert upgraded.execute("SELECT version FROM schema_version").fetchone()[0] == 12
        assert list((tmp_path / "backups").glob("devmem-before-v12-from-v11-*.db"))
    finally:
        upgraded.close()
