"""Maintenance tests: a fake engine, temporary SQLite/git state, stubbed subprocesses. Never the real codex/claude or ~/.devmem."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from devmem import cli, maintenance, mcp_server
from devmem.db import EMBEDDING_DIM, EMBEDDING_MODEL, _V7_STATEMENTS, audit_memories, connect, connection, context_text, embedding_hash, hybrid_search, now
from devmem.embed import set_embedder


# ---------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def _fresh_mcp_client(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_id", None); monkeypatch.setattr(mcp_server, "_client_name", None)


@pytest.fixture(autouse=True)
def _maintenance_enabled():
    _config(enabled=True)


class FakeEngine:
    name = "fake"; model = None
    def __init__(self, proposals=None, ok=True):
        self.proposals, self.ok, self.proposal_calls, self.verify_calls = proposals or {}, ok, [], []
    def proposal(self, payload):
        self.proposal_calls.append(payload); mid = payload["memory"]["id"]; value = self.proposals.get(mid)
        if callable(value): return value(payload)
        return json.loads(json.dumps(value)) if value else _flag(mid)
    def verify(self, payload):
        self.verify_calls.append(payload); return {"ok": self.ok, "reason": "canned"}


def _ago(**delta) -> str: return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _memory(content="branch old, WIP", kind="note", tags="", project=None, title="old", days=5, review_after=None, pinned=0):
    with connection() as conn:
        return conn.execute("INSERT INTO memories(project,kind,title,content,tags,pinned,review_after,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (project, kind, title, content, tags, pinned, review_after, _ago(days=days), _ago(days=days))).lastrowid


def _proposal(mid, content="branch old is done", **extra):
    return {"action": "update", "memory_id": mid, "target_id": None, "new_title": "new", "new_content": content,
            "new_review_after": None, "dropped": [], "evidence": [{"ref": "wording", "detail": "stale"}], "summary": "updated", **extra}


def _flag(mid): return {"action": "flag", "memory_id": mid, "target_id": None, "new_title": None, "new_content": None, "new_review_after": None, "dropped": [], "evidence": [], "summary": "flag"}
def _keep(mid): return {**_flag(mid), "action": "keep", "summary": "still valid"}
def _row(mid):
    with connection() as conn: return dict(conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())
def _changes(mid=None):
    with connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM maintenance_changes" + (" WHERE memory_id=?" if mid else "") + " ORDER BY id", (mid,) if mid else ())]
def _runs():
    with connection() as conn: return [dict(r) for r in conn.execute("SELECT * FROM maintenance_runs ORDER BY id")]
def _link(a, b, kind="overlaps", note=""):
    with connection() as conn: return conn.execute("INSERT INTO memory_links(from_id,to_id,type,note,created_at) VALUES(?,?,?,?,?)", (a, b, kind, note, now())).lastrowid
def _config(**values): Path(os.environ["DEVMEM_MAINTAIN"]).write_text(json.dumps({"enabled": True, **values}), encoding="utf-8")  # maintenance is opt-in; tests opt in unless they pass enabled=False
def _hash():
    with connection() as conn:
        return hashlib.sha256(json.dumps([[list(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")] for t in ("memories", "memory_links", "memory_revisions")]).encode()).hexdigest()


def _git(repo, *args): return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(path: Path, default="main") -> Path:
    path.mkdir(); _git(path, "init", "-q", "-b", default); _git(path, "config", "user.email", "x@y.z"); _git(path, "config", "user.name", "x")
    (path / "a.txt").write_text("a"); _git(path, "add", "."); _git(path, "commit", "-q", "-m", "a"); return path


def _commit(repo, name):
    (repo / name).write_text(name); _git(repo, "add", "."); _git(repo, "commit", "-q", "-m", name)


def test_maintenance_paths_are_isolated(tmp_path):
    # Item 20: no test may reach the real ~/.devmem lock, config or log.
    assert maintenance.lock_path().parent == tmp_path and maintenance.config_path().parent == tmp_path and cli._maintain_log().parent == tmp_path


# ---------------------------------------------------------------- critical regressions

@pytest.mark.parametrize("kind,tags,cfg", [("decision", "", {}), ("preference", "", {}), ("note", "x,no-auto", {}), ("todo", "", {"protected_kinds": ["todo"]}), ("decision", "", {"protected_kinds": ["todo"]})])
def test_keep_never_touches_protected_memories(kind, tags, cfg):
    # Item 1 regression: keep used to rewrite review_after of decisions/preferences/no-auto memories.
    if cfg: _config(**cfg)
    mid = _memory("Always use X", kind=kind, tags=tags, review_after="2020-01-01")
    result = maintenance.run(engine=FakeEngine({mid: _keep(mid)}))
    assert result["applied"] == 0 and _row(mid)["review_after"] == "2020-01-01"
    assert _changes(mid)[-1]["blocked_by"] == "protected"


@pytest.mark.parametrize("setup,expected", [("per_memory", "per_memory_limit"), ("revert", "recent_revert"), ("edit", "recent_edit")])
def test_keep_obeys_per_memory_guards(setup, expected):
    mid = _memory("Plain fact", review_after="2020-01-01")
    with connection() as conn:
        if setup == "per_memory": conn.execute("INSERT INTO maintenance_changes(run_id,memory_id,action,applied,summary,evidence,created_at) VALUES(0,?,'keep',1,'x','[]',?)", (mid, now()))
        if setup == "revert": conn.execute("INSERT INTO maintenance_changes(run_id,memory_id,action,applied,summary,evidence,created_at,reverted_at) VALUES(0,?,'keep',1,'x','[]',?,?)", (mid, _ago(days=3), now()))
        if setup == "edit": conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (_ago(minutes=30), mid))
    engine = FakeEngine({mid: _keep(mid)}); maintenance.run(engine=engine)
    assert _row(mid)["review_after"] == "2020-01-01"
    if setup == "per_memory": assert engine.proposal_calls == []  # skipped before the model: the guard would block anything
    else: assert _changes(mid)[-1]["blocked_by"] == expected


def test_model_summary_cannot_skip_the_verifier():
    # Item 2 regression: a model summary of "resolved relative dates" used to bypass the verifier.
    mid = _memory(); engine = FakeEngine({mid: _proposal(mid, "Release shipped to every customer", summary="resolved relative dates")}, ok=False)
    maintenance.run(engine=engine)
    assert len(engine.verify_calls) == 1 and _row(mid)["content"] == "branch old, WIP" and _changes(mid)[-1]["blocked_by"] == "verifier"


def test_relative_date_is_evidence_for_the_model_never_an_automatic_rewrite():
    # fix(2026-10-28): "today" meaning "currently" was rewritten to a fixed date without any model or verifier.
    mid = _memory("today Core gets 500")
    engine = FakeEngine()
    assert maintenance.run(engine=engine)["applied"] == 0 and _row(mid)["content"] == "today Core gets 500"
    # No computed date: updated_at is often a later bulk edit, not when "today" was written.
    assert [item["result"] for item in engine.proposal_calls[0]["evidence"] if item["check"] == "relative-date"] == ["relative date words present: today"]


def test_model_date_rewrite_still_needs_the_verifier():
    mid = _memory("deploy shipped yesterday")
    date = (datetime.now(UTC) - timedelta(days=6)).date().isoformat()
    rewrite = {**_proposal(mid, f"deploy shipped {date}"), "new_title": None, "evidence": [{"ref": "relative-date", "detail": "past event"}]}
    rejected = FakeEngine({mid: rewrite}, ok=False)
    maintenance.run(engine=rejected)
    assert len(rejected.verify_calls) == 1 and _row(mid)["content"] == "deploy shipped yesterday" and _changes(mid)[-1]["blocked_by"] == "verifier"


@pytest.mark.parametrize("evidence", [[{"ref": "transcript:5", "detail": "forged"}], [{"ref": "relative-date", "detail": "forged"}], [{"ref": "audit", "detail": "old"}]])
def test_model_supplied_evidence_does_not_count(evidence):
    # Item 3 regression: forged transcript/relative-date refs and audit age findings used to satisfy the evidence guard.
    mid = _memory("Plain fact about the parser", days=40)
    maintenance.run(engine=FakeEngine({mid: _proposal(mid, "Plain fact about the new parser", evidence=evidence)}))
    assert _row(mid)["content"] == "Plain fact about the parser" and _changes(mid)[-1]["blocked_by"] == "evidence"


def test_real_transcript_citation_counts_and_unknown_line_does_not(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps({"type": t, "message": {"role": t, "content": c}}) for t, c in [("user", "is the parser done?"), ("assistant", "yes, the parser shipped")]))
    good, bad = _memory("Plain fact about the parser", days=40), _memory("Another parser fact", days=40)
    engine = FakeEngine({good: _proposal(good, "Plain fact about the parser: shipped", evidence=[{"ref": "transcript:2", "detail": "shipped"}]),
                         bad: _proposal(bad, "Another parser fact: shipped", evidence=[{"ref": "transcript:99", "detail": "forged"}])})
    result = maintenance.run("session_end", engine=engine, transcript_path=str(transcript))
    assert result["applied"] == 1 and _row(good)["content"].endswith("shipped") and _row(bad)["content"] == "Another parser fact"
    assert _changes(bad)[-1]["blocked_by"] == "evidence" and "transcript:2 yes, the parser shipped" in engine.verify_calls[0]["transcript_lines"][0]


@pytest.mark.parametrize("old,new,dropped,blocked", [
    ("WIP see !42 here", "see the issue here", [{"token": "!42", "reason": "per model-ref"}], "information"),  # reason names a model ref only
    ("WIP fix #1 now", "fix #12 now done", [], "information"),  # '#1' must not match inside '#12'
    # fix(2026-10-28): "wording" (and "relative-date") only say some words are present; naming them never justifies a drop.
    ("WIP see !42 here", "see the issue here", [{"token": "!42", "reason": "obsolete per wording check"}], "information"),
])
def test_information_guard_uses_exact_tokens_and_code_refs(old, new, dropped, blocked):
    mid = _memory(old)
    maintenance.run(engine=FakeEngine({mid: _proposal(mid, new, dropped=dropped, evidence=[{"ref": "model-ref", "detail": "x"}])}))
    assert (_row(mid)["content"] == old) == bool(blocked)
    assert _changes(mid)[-1]["blocked_by"] == blocked


def test_drop_reason_naming_a_mechanical_ref_is_accepted(tmp_path):
    project = tmp_path / "p"; project.mkdir()
    mid = _memory("WIP see !42 in docs/gone.md", project=str(project))
    dropped = [{"token": token, "reason": "docs/gone.md is missing"} for token in ("!42", "docs/gone.md")]
    maintenance.run(engine=FakeEngine({mid: _proposal(mid, "see the issue here, file removed", dropped=dropped)}))
    assert _row(mid)["content"] == "see the issue here, file removed" and _changes(mid)[-1]["applied"] == 1


SECRETS = ["api_key=sk_live_ABCDEFGH12345", "ghp_" + "A" * 36, "sk-" + "a" * 32, "AKIA" + "B" * 16, "xoxb-1234567890-abc", "eyJhbGciOiJI.eyJzdWIiOiIx.c2lnbmF0dXJl", "-----BEGIN RSA PRIVATE KEY-----", "password: hunter2hunter2"]


@pytest.mark.parametrize("secret", SECRETS)
def test_secret_patterns(secret): assert maintenance.SECRET.search(f"text {secret} text")


@pytest.mark.parametrize("field", ["new_title", "new_content", "summary", "evidence", "dropped"])
@pytest.mark.parametrize("kind", ["note", "decision"])
def test_secrets_are_never_persisted(field, kind):
    # Item 4 regression: secrets in titles, summaries, evidence and protected-memory flags used to be stored.
    secret = "ghp_" + "Z" * 36; mid = _memory(kind=kind, pinned=1)
    proposal = _proposal(mid)
    if field in ("new_title", "new_content", "summary"): proposal[field] = f"{proposal[field]} {secret}"
    elif field == "evidence": proposal["evidence"].append({"ref": "x", "detail": "token " + secret})
    else: proposal["dropped"] = [{"token": "x", "reason": secret}]
    maintenance.run(engine=FakeEngine({mid: proposal}))
    change = _changes(mid)[-1]
    assert change["blocked_by"] == "secret" and change["new_title"] is None and change["new_content"] is None
    assert secret not in json.dumps(_changes()) and secret not in json.dumps(_runs()) and _row(mid)["content"] == "branch old, WIP"


@pytest.mark.parametrize("text,error", [
    ('{"enabled": false,}', "not valid JSON"), ('["x"]', "JSON object"), ('{"protected_kinds": "preference"}', "protected_kinds"),
    ('{"max_shrink": 1}', "max_shrink"), ('{"full_every_hours": "x"}', "full_every_hours"), ('{"enabeld": false}', "unknown key"),
    ('{"max_changes": true}', "max_changes"), ('{"engine": "gpt"}', "engine"),
])
def test_bad_config_disables_maintenance_and_records_an_error(text, error, monkeypatch):
    # Item 5 regression: malformed config used to silently re-enable maintenance and drop protections.
    Path(os.environ["DEVMEM_MAINTAIN"]).write_text(text)
    cfg = maintenance.config(); assert cfg["enabled"] is False and error in cfg["error"]
    mid = _memory(); engine = FakeEngine({mid: _proposal(mid)})
    result = maintenance.run(engine=engine)
    assert result["status"] == "error" and error in _runs()[-1]["error"] and not engine.proposal_calls and _row(mid)["content"] == "branch old, WIP"
    assert maintenance.due_for_startup() is True  # does not crash; the spawned run records the error
    calls = []; monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: calls.append(a))
    monkeypatch.setattr("uvicorn.Server.run", lambda self, *a, **k: None); monkeypatch.setattr("sys.argv", ["devmem", "panel"]); cli.main()


def test_config_defaults_and_protected_kinds_can_only_grow():
    Path(os.environ["DEVMEM_MAINTAIN"]).unlink()
    assert maintenance.config() == maintenance.DEFAULTS
    _config(protected_kinds=["todo"]); assert set(maintenance.config()["protected_kinds"]) == {"todo", "decision", "preference"}
    _config(enabled=False); assert maintenance.run(engine=FakeEngine())["status"] == "disabled" and not maintenance.due_for_startup()


def _completed(rc=0, stdout="", stderr=""): return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def test_engine_errors_are_sanitized(monkeypatch, tmp_path):
    # Item 6 regression: CalledProcessError text contained the whole prompt (memory + transcript); stderr was dropped.
    payload = {"memory": {"id": 1, "project": str(tmp_path), "content": "SENSITIVE-MEMORY-TEXT"}}
    for effect, message in [(_completed(2, stderr="model overloaded"), "exited with code 2: model overloaded"), (subprocess.TimeoutExpired(["codex"], 300), "timed out")]:
        def fake(command, effect=effect, **kwargs):
            if isinstance(effect, Exception): raise effect
            return effect
        monkeypatch.setattr(maintenance.subprocess, "run", fake)
        with pytest.raises(RuntimeError) as caught: maintenance.Engine("codex").proposal(payload)
        assert message in str(caught.value) and "SENSITIVE" not in str(caught.value)
    monkeypatch.setattr(maintenance.subprocess, "run", lambda *a, **k: _completed(0, json.dumps({"is_error": True, "result": "quota"})))
    with pytest.raises(RuntimeError, match="claude reported an error: quota"): maintenance.Engine("claude").proposal(payload)
    def missing(*a, **k): raise FileNotFoundError("codex")
    monkeypatch.setattr(maintenance.subprocess, "run", missing)
    with pytest.raises(maintenance.EngineUnavailable): maintenance.Engine("codex").proposal(payload)


def test_real_engine_failure_is_an_error_run_without_changes(monkeypatch):
    mid = _memory("SENSITIVE-MEMORY-TEXT WIP")
    monkeypatch.setattr(maintenance.subprocess, "run", lambda *a, **k: _completed(1, stderr="boom"))
    result = maintenance.run(engine=maintenance.Engine("codex"))
    assert result["status"] == "error" and "exited with code 1: boom" in _runs()[-1]["error"] and "SENSITIVE" not in _runs()[-1]["error"]
    assert _row(mid)["content"] == "SENSITIVE-MEMORY-TEXT WIP" and not _changes()
    def missing(*a, **k): raise FileNotFoundError("codex")
    monkeypatch.setattr(maintenance.subprocess, "run", missing)
    other = _memory("other WIP")
    result = maintenance.run(engine=maintenance.Engine("codex"))
    assert result["status"] == "error" and "binary not found" in result["error"] and not _changes(other)  # not the memory's fault


def test_engine_unavailable_waits_for_the_normal_startup_interval_without_flagging():
    mid = _memory()
    class MissingEngine:
        name, model = "codex", None
        def proposal(self, payload): raise maintenance.EngineUnavailable("codex binary not found on PATH")
    result = maintenance.run(engine=MissingEngine())
    assert result["status"] == "error" and not maintenance.due_for_startup() and not _changes(mid)
    _config(enabled=False)
    with connection() as conn: assert "Automatic maintenance failing" not in context_text(conn, None)


def test_engine_process_failure_keeps_startup_due_and_blames_no_memory():
    mid = _memory()
    class FailingEngine:
        name, model = "codex", None
        def proposal(self, payload): raise maintenance.EngineRunFailed("codex exited with code 1: quota exceeded")
    result = maintenance.run(engine=FailingEngine())
    assert result["status"] == "error" and result["error"].startswith("codex exited") and maintenance.due_for_startup() and not _changes(mid)


def test_codex_command_uses_stdin_schema_model_and_child_env(monkeypatch, tmp_path):
    # Items 9 and 23: prompt on stdin (never argv), child marker in env, schema file content.
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs, json.loads(Path(command[command.index("--output-schema") + 1]).read_text())))
        Path(command[command.index("-o") + 1]).write_text(json.dumps(_proposal(1)))
        return _completed()
    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)
    assert maintenance.Engine("codex", "test-model").proposal({"memory": {"id": 1, "project": str(tmp_path), "content": "PROMPT-MARKER"}})["memory_id"] == 1
    command, kwargs, schema = calls[0]
    assert command[:6] == ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-C"] and command[6] == str(tmp_path)
    assert command[command.index("--model") + 1] == "test-model" and command[-1] == "-" and not any("PROMPT-MARKER" in part for part in command)
    assert "PROMPT-MARKER" in kwargs["input"] and kwargs["env"][maintenance.CHILD_ENV] == "1" and kwargs["timeout"] == 300
    assert set(schema["required"]) >= {"action", "memory_id", "dropped", "evidence"}


def test_claude_command_parses_fenced_result(monkeypatch, tmp_path):
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs)); return _completed(0, json.dumps({"is_error": False, "result": "```json\n" + json.dumps({"ok": True, "reason": ""}) + "\n```"}))
    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)
    assert maintenance.Engine("claude", "sonnet").verify({"memory": {"project": str(tmp_path)}, "old_text": "PROMPT-MARKER"}) == {"ok": True, "reason": ""}
    command, kwargs = calls[0]
    assert command == ["claude", "-p", "--output-format", "json", "--model", "sonnet"] and kwargs["cwd"] == tmp_path
    assert "PROMPT-MARKER" in kwargs["input"] and '"ok"' in kwargs["input"] and kwargs["env"][maintenance.CHILD_ENV] == "1"


# ---------------------------------------------------------------- run structure, requests, locks

def test_engine_calls_hold_no_write_lock():
    # Item 7 regression: the whole run used to be one write transaction, so MCP/panel writes failed "database is locked".
    first, second = _memory(), _memory("second WIP"); wrote = []
    def probe(payload):
        conn = sqlite3.connect(os.environ["DEVMEM_DB"], timeout=0.2)
        conn.execute("INSERT INTO tool_calls(tool,ok,at) VALUES('probe',1,?)", (now(),)); conn.commit(); conn.close()
        wrote.append(1); return _proposal(second, "second is done")
    assert maintenance.run(engine=FakeEngine({first: _flag(first), second: probe}))["applied"] == 1 and wrote == [1]


def test_failure_on_a_later_memory_rolls_back_earlier_changes():
    first, second = _memory(), _memory("second WIP")
    def broken(payload): raise ValueError("bad json")
    result = maintenance.run(engine=FakeEngine({first: _proposal(first), second: broken}))
    assert result["status"] == "error" and "bad json" in result["error"]
    assert _row(first)["content"] == "branch old, WIP" and not [c for c in _changes() if c["applied"]]


def test_bad_memory_is_skipped_by_the_next_run():
    # Item 19: one memory with a wrong memory_id used to fail every run forever.
    bad, good = _memory(), _memory("good WIP")
    engine = FakeEngine({bad: {**_proposal(bad), "memory_id": 9999}, good: _proposal(good, "good is done")})
    assert maintenance.run(engine=engine)["status"] == "error" and _changes(bad)[-1]["blocked_by"] == "engine_error"
    engine.proposal_calls.clear()
    assert maintenance.run(engine=engine)["applied"] == 1 and _row(good)["content"] == "good is done"
    assert [p["memory"]["id"] for p in engine.proposal_calls] == [good]
    assert maintenance.dismiss(_changes(bad)[-1]["id"])  # dismissing the error flag means "try again"
    engine.proposals[bad] = _proposal(bad); engine.proposal_calls.clear()
    assert maintenance.run(engine=engine)["applied"] == 1 and [p["memory"]["id"] for p in engine.proposal_calls] == [bad]


def test_requests_while_locked_are_safe_and_coalesced(tmp_path):
    # Item 8 regression: a dry run queued while locked was later drained as a real run.
    project = str(tmp_path / "p"); os.mkdir(project); mid = _memory(project=project)
    t1, t2 = tmp_path / "t1.jsonl", tmp_path / "t2.jsonl"
    t2.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "newest session"}}))
    engine = FakeEngine({mid: _proposal(mid)})
    with maintenance.Lock(maintenance.lock_path()) as lock:
        assert lock.acquired
        assert maintenance.run(dry_run=True, engine=engine) == {"status": "locked", "checked": 0, "applied": 0, "flagged": 0}
        assert maintenance.run(limit=1, engine=engine)["status"] == "locked"
        for _ in range(5): maintenance.run("startup", engine=engine)
        maintenance.run("session_end", project=project, transcript_path=str(t1), engine=engine)
        maintenance.run("session_end", project=project, transcript_path=str(t2), engine=engine)
    requested = [r for r in _runs() if r["status"] == "requested"]
    assert [(r["trigger"], r["project"]) for r in requested] == [("startup", None), ("session_end", project)]
    assert json.loads(requested[1]["options"]) == {"transcript": str(t2)} and _row(mid)["content"] == "branch old, WIP"
    assert maintenance.run(engine=FakeEngine())["status"] == "success"  # full run; then drains
    statuses = {r["id"]: r["status"] for r in _runs()}
    assert statuses[requested[0]["id"]] == "skipped" and statuses[requested[1]["id"]] == "done"
    assert [r for r in _runs() if r["trigger"] == "startup" and r["status"] == "success"] == []


def test_drained_session_request_uses_its_transcript(tmp_path):
    project = str(tmp_path / "p"); os.mkdir(project); mid = _memory(project=project)
    transcript = tmp_path / "t.jsonl"; transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "newest session"}}))
    with maintenance.Lock(maintenance.lock_path()):  # engine= too: if the lock ever failed, this must not build the real codex engine
        maintenance.run("session_end", project=project, transcript_path=str(transcript), engine=FakeEngine())
    engine = FakeEngine({mid: _proposal(mid)})
    maintenance.run(project=str(tmp_path / "elsewhere"), engine=engine)
    assert engine.proposal_calls[0]["transcript"] == "transcript:1 newest session" and _row(mid)["content"] == "branch old is done"


def test_lock_uses_flock(tmp_path):
    # Item 18: garbled files and PID reuse used to block forever; flock releases with the process.
    path = tmp_path / "lock"
    for content in ("", "garbage", str(os.getppid())):
        path.write_text(content)
        with maintenance.Lock(path) as lock: assert lock.acquired and path.read_text() == str(os.getpid())
    with maintenance.Lock(path) as held:
        with maintenance.Lock(path) as second: assert held.acquired and not second.acquired
    with maintenance.Lock(path) as again: assert again.acquired


def test_unusable_lock_and_crashes_never_leave_running_rows(tmp_path, monkeypatch):
    # Item 14 regression.
    (tmp_path / "file").write_text("x"); monkeypatch.setenv("DEVMEM_MAINTAIN_LOCK", str(tmp_path / "file" / "lock"))
    assert "lock unavailable" in maintenance.run(engine=FakeEngine())["error"] and _runs()[-1]["status"] == "error"
    monkeypatch.setenv("DEVMEM_MAINTAIN_LOCK", str(tmp_path / "lock"))
    def boom(conn): raise RuntimeError("audit exploded")
    audit = maintenance.audit_memories
    monkeypatch.setattr(maintenance, "audit_memories", boom)
    assert maintenance.run(engine=FakeEngine())["status"] == "error" and _runs()[-1]["status"] == "error"
    # Restore only the patched function: monkeypatch.undo() would also drop conftest's temp DEVMEM_* paths.
    monkeypatch.setattr(maintenance, "audit_memories", audit)
    mid = _memory()
    def interrupt(payload): raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt): maintenance.run(engine=FakeEngine({mid: interrupt}))
    assert _runs()[-1]["status"] == "error" and "interrupted" in _runs()[-1]["error"]
    with connection() as conn: orphan = conn.execute("INSERT INTO maintenance_runs(trigger,started_at,status) VALUES('manual',?,'running')", (now(),)).lastrowid
    maintenance.run(project="/nothing", engine=FakeEngine())
    assert {r["id"]: r for r in _runs()}[orphan]["status"] == "error"
    assert not [r for r in _runs() if r["status"] == "running"]


# ---------------------------------------------------------------- guardrails at run() level

def _partner_pair(project, kind="note", other_content="other WIP note"):
    survivor, other = _memory(project=project), _memory(other_content, project=project, kind=kind); _link(survivor, other); return survivor, other


@pytest.mark.parametrize("case,expected", [
    ("recent_edit", "recent_edit"), ("review_after", "review_after"), ("keep_reason", "keep_reason"), ("content", "content"),
    ("size", "size"), ("shrink", "shrink"), ("action", "action"), ("verifier", "verifier"), ("evidence", "evidence"),
])
def test_each_guardrail_blocks_and_leaves_memory_unchanged(case, expected, tmp_path):
    content = "see docs/gone.md WIP " + "x" * 80 if case == "shrink" else "see docs/gone.md WIP"
    mid = _memory(content, project=str(tmp_path), days=40 if case == "evidence" else 5)
    if case == "evidence":
        mid = _memory("Plain fact", days=40)
    if case == "recent_edit":
        with connection() as conn: conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (_ago(minutes=30), mid))
    proposal = _proposal(mid, "see docs/gone.md, now removed")
    proposal.update({"review_after": {"new_review_after": "next week"}, "keep_reason": {"action": "keep"}, "content": {"new_content": None},
                     "size": {"new_content": "docs/gone.md " + "x" * 2001}, "shrink": {"new_content": "docs/gone.md"}, "action": {"action": "delete"},
                     "evidence": {"new_content": "Plain fact, revised"}}.get(case, {}))
    before = _row(mid)
    maintenance.run(engine=FakeEngine({mid: proposal}, ok=case != "verifier"))
    after = _row(mid)
    assert (after["content"], after["title"], after["review_after"]) == (before["content"], before["title"], before["review_after"])
    assert _changes(mid)[-1]["blocked_by"] == expected and _changes(mid)[-1]["applied"] == 0


@pytest.mark.parametrize("case", ["self", "not_partner", "other_project", "superseded"])
def test_target_guardrail(case, tmp_path):
    project = str(tmp_path); survivor, other = _partner_pair(project)
    target = {"self": survivor, "not_partner": _memory("x WIP", project=project), "other_project": _memory("x WIP", project="/elsewhere"), "superseded": other}[case]
    if case == "other_project": _link(survivor, target)
    if case == "superseded": _link(_memory("newest", project=project), other, "supersedes")
    maintenance.run(project=project, engine=FakeEngine({survivor: {**_proposal(survivor, "branch old, WIP; other WIP note"), "action": "merge", "target_id": target}}))
    assert _changes(survivor)[-1]["blocked_by"] == "target" and _row(survivor)["content"] == "branch old, WIP"


@pytest.mark.parametrize("side", ["memory", "target"])
def test_protected_on_either_side_of_supersede(side, tmp_path):
    project = str(tmp_path); memory, target = _partner_pair(project, kind="decision" if side == "target" else "note")
    if side == "memory":
        with connection() as conn: conn.execute("UPDATE memories SET tags='no-auto' WHERE id=?", (memory,))
    maintenance.run(project=project, engine=FakeEngine({memory: {**_flag(memory), "action": "supersede", "target_id": target, "evidence": [{"ref": "wording", "detail": "x"}]}}))
    assert _changes(memory)[-1]["blocked_by"] == "protected"
    with connection() as conn: assert not conn.execute("SELECT 1 FROM memory_links WHERE type='supersedes'").fetchone()


def test_per_memory_limit_at_run_level():
    mid = _memory(); engine = FakeEngine({mid: _proposal(mid)})
    assert maintenance.run(engine=engine)["applied"] == 1
    with connection() as conn: conn.execute("UPDATE memories SET content='branch old, WIP',updated_at=? WHERE id=?", (_ago(days=5), mid))
    engine.proposals[mid] = _proposal(mid, "branch old is really done")
    calls = len(engine.proposal_calls)
    assert maintenance.run(engine=engine)["applied"] == 0
    assert len(engine.proposal_calls) == calls and _row(mid)["content"] == "branch old, WIP"


def test_a_dismissed_flag_is_not_a_revert():
    mid = _memory()
    with connection() as conn:
        conn.execute("INSERT INTO maintenance_changes(run_id,memory_id,action,applied,summary,evidence,blocked_by,created_at,reverted_at) VALUES(0,?,'flag',0,'x','[]','verifier',?,?)", (mid, _ago(days=8), now()))
    assert maintenance.run(engine=FakeEngine({mid: _proposal(mid)}))["applied"] == 1 and _row(mid)["content"] == "branch old is done"


def test_max_changes_caps_a_run_without_extra_engine_calls():
    _config(max_changes=2); ids = [_memory(f"item {n} WIP") for n in range(3)]
    engine = FakeEngine({mid: _proposal(mid, f"item {n} is done") for n, mid in enumerate(ids)})
    result = maintenance.run(engine=engine)
    assert result["applied"] == 2 and _row(ids[2])["content"] == "item 2 WIP" and len(engine.proposal_calls) == 2
    assert "max_changes reached: 1" in result["note"] and "max_changes reached" in _runs()[-1]["note"]


def test_protected_memories_are_not_reflagged(tmp_path):
    # Item 26: the same protected memory used to be re-flagged (and re-sent to the engine) every run.
    mid = _memory("see docs/gone.md", kind="decision", project=str(tmp_path), pinned=1)
    engine = FakeEngine({mid: _proposal(mid, "docs/gone.md was removed")})
    maintenance.run(engine=engine); maintenance.run(engine=engine)
    assert len(engine.proposal_calls) == 1
    open_flags = lambda: [c for c in _changes(mid) if c["action"] == "flag" and c["reverted_at"] is None]
    assert len(open_flags()) == 1 and open_flags()[0]["blocked_by"] == "protected"
    with connection() as conn:  # the user edits the rule after the flag: it is re-checked, and only the new flag stays open
        conn.execute("UPDATE maintenance_changes SET created_at=? WHERE memory_id=?", (_ago(hours=5), mid)); conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (_ago(hours=3), mid))
    maintenance.run(engine=engine)
    assert len(engine.proposal_calls) == 2 and len(open_flags()) == 1 and len(_changes(mid)) == 2


# ---------------------------------------------------------------- apply and revert

def test_update_applies_and_reverts_content():
    mid = _memory(); maintenance.run(engine=FakeEngine({mid: _proposal(mid)}))
    assert (_row(mid)["title"], _row(mid)["content"]) == ("new", "branch old is done")
    change = _changes(mid)[-1]; assert change["action"] == "update" and change["applied"] == 1 and change["revision_id"]
    assert maintenance.revert(change["id"]) and (_row(mid)["title"], _row(mid)["content"]) == ("old", "branch old, WIP")
    assert not maintenance.revert(change["id"])


def test_merge_applies_supersedes_target_and_reverts(tmp_path):
    project = str(tmp_path); survivor, other = _partner_pair(project)
    engine = FakeEngine({survivor: {**_proposal(survivor, "branch old, WIP; other WIP note"), "action": "merge", "target_id": other}})
    assert maintenance.run(project=project, engine=engine)["applied"] == 1
    assert "other WIP note" in engine.verify_calls[0]["old_text"] and engine.proposal_calls[0]["partners"][0]["content"] == "other WIP note"
    assert _row(survivor)["content"] == "branch old, WIP; other WIP note" and _row(other)["content"] == "other WIP note"
    with connection() as conn: assert [tuple(r) for r in conn.execute("SELECT from_id,to_id FROM memory_links WHERE type='supersedes'")] == [(survivor, other)]
    assert mcp_server.memory_get(other)["superseded_by"] == survivor
    change = [c for c in _changes(survivor) if c["action"] == "merge"][0]
    assert maintenance.revert(change["id"]) and _row(survivor)["content"] == "branch old, WIP" and mcp_server.memory_get(other)["superseded_by"] is None


@pytest.mark.parametrize("new,expected", [("branch old, WIP", "information"), ("branch", "shrink")])
def test_merge_checks_the_targets_information(new, expected, tmp_path):
    # Item 10 regression: the target's text was not in the token set or the shrink baseline.
    project = str(tmp_path); survivor, other = _partner_pair(project, other_content="other WIP !42")
    maintenance.run(project=project, engine=FakeEngine({survivor: {**_proposal(survivor, new), "action": "merge", "target_id": other}}))
    assert _changes(survivor)[-1]["blocked_by"] == expected and not mcp_server.memory_get(other)["superseded_by"]


def test_supersede_links_newer_to_older_and_reverts(tmp_path):
    project = str(tmp_path); older, newer = _partner_pair(project)
    maintenance.run(project=project, engine=FakeEngine({older: {**_flag(older), "action": "supersede", "target_id": newer, "evidence": [{"ref": "wording", "detail": "x"}]}}))
    assert mcp_server.memory_get(older)["superseded_by"] == newer and _row(older)["content"] == "branch old, WIP"
    assert maintenance.revert(_changes(older)[-1]["id"]) and mcp_server.memory_get(older)["superseded_by"] is None


def test_keep_pushes_review_date_and_revert_restores_it():
    mid = _memory("Plain fact", review_after="2020-01-01")
    maintenance.run(engine=FakeEngine({mid: _keep(mid)}))
    assert _row(mid)["review_after"] == (datetime.now(UTC) + timedelta(days=14)).date().isoformat() and _row(mid)["content"] == "Plain fact"
    assert maintenance.revert(_changes(mid)[-1]["id"]) and _row(mid)["review_after"] == "2020-01-01"


def test_revert_never_wipes_an_unrelated_user_edit():
    # Item 13 regression: an update that left title/content alone picked an older revision, so revert erased the user's edit.
    mid = _memory("WIP original")
    with connection() as conn: conn.execute("UPDATE memories SET content='WIP user edit',updated_at=? WHERE id=?", (_ago(days=5), mid))
    maintenance.run(engine=FakeEngine({mid: {**_proposal(mid, "WIP user edit"), "new_title": None, "new_review_after": "2030-01-01"}}))
    change = _changes(mid)[-1]; assert change["applied"] == 1 and change["revision_id"] is None and _row(mid)["review_after"] == "2030-01-01"
    assert maintenance.revert(change["id"]) and _row(mid)["content"] == "WIP user edit" and _row(mid)["review_after"] is None


def test_revert_refuses_after_a_later_edit_and_keeps_reused_link_ids(tmp_path):
    mid = _memory(); maintenance.run(engine=FakeEngine({mid: _proposal(mid)}))
    with connection() as conn: conn.execute("UPDATE memories SET content='user text',updated_at=? WHERE id=?", (now(), mid))
    with pytest.raises(ValueError, match="edited after"): maintenance.revert(_changes(mid)[-1]["id"])
    assert _row(mid)["content"] == "user text"
    project = str(tmp_path); survivor, other = _partner_pair(project)
    maintenance.run(project=project, engine=FakeEngine({survivor: {**_flag(survivor), "action": "supersede", "target_id": other, "evidence": [{"ref": "wording", "detail": "x"}]}}))
    change = _changes(survivor)[-1]; a, b = _memory("a"), _memory("b")
    with connection() as conn:
        conn.execute("DELETE FROM memory_links WHERE id=?", (change["link_id"],))
        assert conn.execute("INSERT INTO memory_links(from_id,to_id,type,created_at) VALUES(?,?,'relates_to',?)", (a, b, now())).lastrowid == change["link_id"]
    assert maintenance.revert(change["id"])
    with connection() as conn: assert conn.execute("SELECT 1 FROM memory_links WHERE from_id=? AND to_id=?", (a, b)).fetchone()


def test_dry_run_changes_nothing_and_lists_proposals(tmp_path, monkeypatch, capsys):
    project = str(tmp_path); update = _memory(project=project); keep = _memory("Plain fact", project=project, review_after="2020-01-01")
    survivor, merged = _partner_pair(project); older, newer = _partner_pair(project)
    proposals = {update: _proposal(update), keep: _keep(keep), survivor: {**_proposal(survivor, "branch old, WIP; other WIP note"), "action": "merge", "target_id": merged},
                 older: {**_flag(older), "action": "supersede", "target_id": newer, "evidence": [{"ref": "wording", "detail": "x"}]}}
    with connection() as conn: conn.execute("INSERT INTO maintenance_runs(trigger,started_at,status) VALUES('session_end',?,'requested')", (now(),))
    before = _hash()
    result = maintenance.run(dry_run=True, engine=FakeEngine(proposals))
    assert result["status"] == "dry_run" and result["proposed"] == 4 and result["applied"] == 0 and _hash() == before
    assert sorted(c["action"] for c in result["changes"] if c["blocked_by"] is None) == ["keep", "merge", "supersede", "update"]
    assert [r["status"] for r in _runs() if r["trigger"] == "session_end"] == ["requested"]  # not drained by a dry run
    assert maintenance.due_for_startup()
    monkeypatch.setattr(maintenance, "Engine", lambda *a, **k: FakeEngine(proposals)); monkeypatch.setattr("sys.argv", ["devmem", "maintain", "--dry-run", "--project", project])
    cli.main(); out = capsys.readouterr().out
    assert f"#{update} update [would apply] updated" in out and "    branch old is done" in out and '"status":"dry_run"' in out.splitlines()[-1]
    assert _hash() == before


# ---------------------------------------------------------------- evidence extraction

def test_merged_branch_updates_memory_with_merge_commit_evidence(tmp_path):
    repo = _repo(tmp_path / "repo"); _git(repo, "checkout", "-q", "-b", "feature/login"); _commit(repo, "login.py")
    _git(repo, "checkout", "-q", "main"); _commit(repo, "other.py"); _git(repo, "merge", "-q", "--no-ff", "feature/login", "-m", "merge login")
    merge = _git(repo, "rev-parse", "--short", "HEAD")
    mid = _memory("Working on feature/login for the login form.", project=str(repo))
    engine = FakeEngine({mid: _proposal(mid, f"feature/login was merged ({merge}).")})
    assert maintenance.run(project=str(repo), engine=engine)["applied"] == 1
    evidence = json.loads(_changes(mid)[-1]["evidence"])
    assert {"ref": "feature/login", "result": "merged", "commit": merge} == {k: v for k, v in evidence[0].items() if k in ("ref", "result", "commit")}


def test_unmerged_and_contained_branches_are_not_evidence(tmp_path):
    repo = _repo(tmp_path / "repo", default="master"); _git(repo, "checkout", "-q", "-b", "feature/open"); _commit(repo, "open.py"); _git(repo, "checkout", "-q", "master")
    _git(repo, "branch", "feature/fresh"); _commit(repo, "later.py")  # fresh is now behind master, with no merge commit
    mid = _memory("Working on feature/open and feature/fresh.", project=str(repo)); engine = FakeEngine({mid: _proposal(mid)})
    maintenance.run(project=str(repo), engine=engine)
    assert engine.proposal_calls == [] and _row(mid)["content"] == "Working on feature/open and feature/fresh."
    evidence, info = maintenance._evidence(_row(mid))
    assert evidence == [] and "without a merge commit" in info[0]["result"]


def test_merged_detection_with_master_default(tmp_path):
    repo = _repo(tmp_path / "repo", default="master"); _git(repo, "checkout", "-q", "-b", "fix/crash"); _commit(repo, "c.py")
    _git(repo, "checkout", "-q", "master"); _commit(repo, "m.py"); _git(repo, "merge", "-q", "--no-ff", "fix/crash", "-m", "merge")
    evidence, _ = maintenance._evidence({**_row(_memory("fix/crash pending", project=str(repo)))})
    assert evidence[0]["result"] == "merged" and evidence[0]["base"] == "master"


def test_extractors_ignore_prose_urls_and_dates(tmp_path, monkeypatch):
    # Item 17 regression: "and/or", URLs, dates and hex words were reported as missing paths/branches/commits.
    repo = _repo(tmp_path / "repo"); commands = []; real = maintenance.subprocess.run
    def spy(command, **kwargs): commands.append((command, kwargs)); return real(command, **kwargs)
    monkeypatch.setattr(maintenance.subprocess, "run", spy)
    text = "Use and/or Q3/Q4 per https://example.com/a/b.html on 10/28; defaced 1234567; branch --force; the branch is merged"
    assert maintenance._evidence(_row(_memory(text, project=str(repo)))) == ([], [])
    assert commands and all(k.get("timeout") for _, k in commands) and not any("--force" in c for c, _ in commands)
    evidence, _ = maintenance._evidence(_row(_memory("see src/missing.py and feature/gone and 1a2b3c4d", project=str(repo))))
    assert {(e["check"], e["ref"], e["result"]) for e in evidence} == {("path", "src/missing.py", "missing"), ("branch", "feature/gone", "missing"), ("commit", "1a2b3c4d", "missing")}


def test_newer_commits_are_context_not_evidence(tmp_path):
    # fix(2026-10-28): "newer commit exists" is true for almost any commit and flagged nearly every memory.
    repo = _repo(tmp_path / "repo"); sha = _git(repo, "rev-parse", "--short=8", "HEAD"); _commit(repo, "later.py")
    evidence, info = maintenance._evidence(_row(_memory(f"fixed in {sha}", project=str(repo))))
    assert evidence == [] and [(i["ref"], i["result"]) for i in info] == [(sha, "newer commit exists")]


def test_path_check_skips_routes_and_finds_monorepo_and_sibling_paths(tmp_path):
    # fix(2026-10-28): module paths inside packages/, API routes and ~/project/knowledge-library notes were reported missing.
    repo = _repo(tmp_path / "repo"); nested = repo / "packages/app/src/app/core"; nested.mkdir(parents=True)
    (nested / "settings.py").write_text("x"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "nested")
    (tmp_path / "knowledge-library").mkdir(); (tmp_path / "knowledge-library/notes.md").write_text("x")
    text = "core/settings.py, app/core/, /v1/extract, knowledge-library/notes.md, core/gone.py, /etc/nope.conf"
    evidence, _ = maintenance._evidence(_row(_memory(text, project=str(repo))))
    assert sorted(e["ref"] for e in evidence) == ["/etc/nope.conf", "core/gone.py"]


def test_none_records_a_closed_row_and_skips_the_memory_for_a_week():
    # fix(2026-10-28): with no "nothing to do" action every false finding became an open flag.
    mid = _memory(); engine = FakeEngine({mid: {**_flag(mid), "action": "none", "summary": "history, still true"}})
    result = maintenance.run(engine=engine)
    assert result["unchanged"] == 1 and result["flagged"] == 0 and _row(mid)["content"] == "branch old, WIP"
    row = _changes(mid)[-1]
    assert row["blocked_by"] == "no_change" and row["reverted_at"] and row["seen_at"] and row["applied"] == 0
    with connection() as conn:
        assert maintenance.attention(conn)["unseen"] == 0
        assert not conn.execute(f"SELECT 1 FROM maintenance_changes c WHERE {maintenance.OPEN_FLAG_SQL}").fetchone()
    maintenance.run(engine=engine); assert len(engine.proposal_calls) == 1  # skipped on the next run
    from devmem.panel.app import app
    assert "history, still true" not in TestClient(app).get("/maintenance").text


def test_memory_changed_in_the_last_day_is_not_sent_to_the_model(tmp_path):
    # fix(2026-10-28): the evidence is a missing path, not age-based wording, so only the 24 h skip can keep the model call away.
    project = tmp_path / "p"; project.mkdir()
    mid = _memory("see docs/gone.md", project=str(project))
    maintenance.run(engine=FakeEngine({mid: _proposal(mid, "docs/gone.md was removed")}))
    assert _row(mid)["content"] == "docs/gone.md was removed"
    with connection() as conn: conn.execute("UPDATE memories SET content='see docs/gone.md', updated_at=? WHERE id=?", (_ago(hours=3), mid))
    engine = FakeEngine({mid: _proposal(mid, "docs/gone.md was removed")}); maintenance.run(engine=engine); assert engine.proposal_calls == []
    with connection() as conn: conn.execute("UPDATE maintenance_changes SET created_at=? WHERE memory_id=?", (_ago(hours=25), mid))
    maintenance.run(engine=engine); assert len(engine.proposal_calls) == 1


def test_no_change_skip_ends_after_seven_days_or_a_user_edit(tmp_path):
    project = tmp_path / "p"; project.mkdir()
    mid = _memory("see docs/gone.md", project=str(project)); engine = FakeEngine({mid: {**_flag(mid), "action": "none", "summary": "fine"}})
    maintenance.run(engine=engine); maintenance.run(engine=engine); assert len(engine.proposal_calls) == 1
    with connection() as conn: conn.execute("UPDATE maintenance_changes SET created_at=? WHERE memory_id=?", (_ago(days=8), mid))
    maintenance.run(engine=engine); assert len(engine.proposal_calls) == 2  # the week is over
    with connection() as conn:  # a user edit after the newest no_change row makes it worth another look
        conn.execute("UPDATE maintenance_changes SET created_at=? WHERE memory_id=?", (_ago(days=1), mid))
        conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (_ago(hours=5), mid))
    maintenance.run(engine=engine); assert len(engine.proposal_calls) == 3


def test_secret_in_a_none_proposal_is_never_stored():
    mid = _memory(); secret = "ghp_" + "Z" * 36
    maintenance.run(engine=FakeEngine({mid: {**_flag(mid), "action": "none", "summary": "fine " + secret}}))
    assert _changes(mid)[-1]["blocked_by"] == "secret" and secret not in json.dumps(_changes())


def test_no_change_row_keeps_no_model_text():
    mid = _memory(); maintenance.run(engine=FakeEngine({mid: {**_flag(mid), "action": "none", "summary": "model words", "new_content": "model text"}}))
    row = _changes(mid)[-1]
    assert (row["summary"], row["new_title"], row["new_content"]) == (maintenance.NO_CHANGE_SUMMARY, None, None)


def test_messages_are_pruned_even_when_maintenance_is_disabled():
    from devmem import messages
    with connection() as conn:
        old = messages.post(conn, "old", sender="a", sender_kind="claude"); messages.close(conn, old)
        conn.execute("UPDATE messages SET created_at=?, closed_at=? WHERE id=?", (_ago(days=40), _ago(days=40), old))
    _config(enabled=False); assert maintenance.run(engine=FakeEngine())["status"] == "disabled"
    with connection() as conn: assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    (Path(os.environ["DEVMEM_MAINTAIN"])).write_text("{broken")
    with connection() as conn:
        again = messages.post(conn, "again", sender="a", sender_kind="claude"); messages.close(conn, again)
        conn.execute("UPDATE messages SET created_at=?, closed_at=? WHERE id=?", (_ago(days=40), _ago(days=40), again))
    assert maintenance.run(engine=FakeEngine())["status"] == "error"
    with connection() as conn: assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_no_test_can_launch_the_real_engines():
    # The conftest guard: a lock or wiring regression must fail loudly instead of spending quota.
    with pytest.raises(AssertionError, match="codex"): subprocess.run(["codex", "exec", "-"], input="x", text=True)
    with pytest.raises(AssertionError, match="claude"): subprocess.Popen(["/usr/local/bin/claude", "-p"])
    with pytest.raises(AssertionError, match="real maintenance engine"): maintenance.Engine("codex").proposal({"memory": {"id": 1}})


def test_global_memories_get_no_filesystem_or_git_checks(monkeypatch):
    # Item 11 regression: global memories were checked against the process cwd's repo.
    calls = []; monkeypatch.setattr(maintenance.subprocess, "run", lambda *a, **k: calls.append(a))
    mid = _memory("see src/missing.py and feature/gone and 1a2b3c4d")
    assert maintenance._evidence(_row(mid)) == ([], []) and calls == []
    engine = FakeEngine({mid: _proposal(mid)}); maintenance.run(engine=engine); assert engine.proposal_calls == []


def test_git_failures_are_check_errors_not_evidence(tmp_path, monkeypatch):
    # Item 16 regression: rc 128 / broken repos were recorded as "missing".
    project = tmp_path / "broken"; project.mkdir(); (project / ".git").write_text("gitdir: /nonexistent")
    evidence, info = maintenance._evidence(_row(_memory("feature/gone 1a2b3c4d", project=str(project))))
    assert evidence == [] and info[0]["check"] == "error"
    repo = _repo(tmp_path / "repo")
    def hang(*a, **k): raise subprocess.TimeoutExpired(a[0], 10)
    monkeypatch.setattr(maintenance.subprocess, "run", hang)
    evidence, info = maintenance._evidence(_row(_memory("feature/gone 1a2b3c4d", project=str(repo))))
    assert evidence == [] and "TimeoutExpired" in info[0]["result"]


def test_paths_are_unverifiable_not_missing_when_git_fails(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo"); real = subprocess.run
    def failing_ls_files(command, *a, **k):
        if "ls-files" in command: return subprocess.CompletedProcess(command, 128, "", "fatal: index file corrupt")
        return real(command, *a, **k)
    monkeypatch.setattr(maintenance.subprocess, "run", failing_ls_files)
    with pytest.raises(maintenance._GitError): maintenance._tracked_suffixes(repo)
    evidence, info = maintenance._evidence(_row(_memory("see core/settings.py and /etc/definitely-missing.conf", project=str(repo))))
    assert [e["ref"] for e in evidence] == ["/etc/definitely-missing.conf"]  # absolute paths need no git
    assert any(i["ref"] == "core/settings.py" and i["result"].startswith("unverifiable") for i in info) and any(i["check"] == "error" for i in info)
    def hang(command, *a, **k):
        if "ls-files" in command: raise subprocess.TimeoutExpired(command, 10)
        return real(command, *a, **k)
    monkeypatch.setattr(maintenance.subprocess, "run", hang); maintenance._SUFFIX_CACHE.clear()
    evidence, info = maintenance._evidence(_row(_memory("see core/settings.py", project=str(repo))))  # caught, not an escaped exception
    assert evidence == [] and any("TimeoutExpired" in i["result"] for i in info)


def test_absolute_dir_under_an_existing_root_is_missing(tmp_path):
    project = tmp_path / "p"; project.mkdir()
    evidence, _ = maintenance._evidence(_row(_memory("logs live in /usr/definitely-missing-dir now", project=str(project))))
    assert [e["ref"] for e in evidence] == ["/usr/definitely-missing-dir"]


# ---------------------------------------------------------------- superseded visibility

def test_superseded_memories_are_hidden_everywhere(tmp_path, monkeypatch):
    project = str(tmp_path); old, new = _memory("needle text", project=project, title="needle old"), _memory("needle text newer", project=project, title="needle new")
    _link(new, old, "supersedes", "automatic maintenance")
    with connection() as conn:
        assert [r["id"] for r in hybrid_search(conn, "needle", where_sql="1=1")] == [new]
        briefing = context_text(conn, project); assert "needle new" in briefing and "needle old" not in briefing
        vector = np.zeros(EMBEDDING_DIM, dtype=np.float32); vector[0] = 1
        for mid, title, content in ((old, "needle old", "needle text"), (new, "needle new", "needle text newer"), (_memory("third", project=project, title="third"), "third", "third")):
            conn.execute("INSERT INTO memory_embeddings(memory_id,model,dim,content_hash,vec) VALUES(?,?,?,?,?)", (mid, EMBEDDING_MODEL, EMBEDDING_DIM, embedding_hash(title, content, ""), vector.tobytes()))
        conn.commit()
        assert all(old not in (f["id"], f.get("other_id")) for f in audit_memories(conn)) and any(f.get("other_id") for f in audit_memories(conn))
        class Fake:
            def embed(self, texts, *, query=False): return np.asarray([vector for _ in texts])
        monkeypatch.delenv("DEVMEM_EMBED"); set_embedder(Fake())
        try:
            results = hybrid_search(conn, "zzzz", where_sql="1=1")
            assert new in [r["id"] for r in results] and old not in [r["id"] for r in results] and {r["match"] for r in results} == {"semantic"}
        finally: set_embedder(None)
    listed = [r["id"] for r in mcp_server.memory_list(scope="all", limit=100)]
    assert new in listed and old not in listed and mcp_server.memory_get(old)["superseded_by"] == new


def test_manual_supersedes_link_keeps_the_older_memory_visible(tmp_path):
    project = str(tmp_path); old, new = _memory("needle text", project=project, title="needle old"), _memory("needle newer", project=project, title="needle new")
    _link(new, old, "supersedes", "user correction")
    with connection() as conn:
        assert {row["id"] for row in hybrid_search(conn, "needle", where_sql="1=1")} == {old, new}
        assert "needle old" in context_text(conn, project)
    assert {row["id"] for row in mcp_server.memory_list(scope="all", limit=100)} >= {old, new}
    assert mcp_server.memory_get(old)["superseded_by"] == new


# ---------------------------------------------------------------- schedule, hooks, CLI

def _full_run(hours_ago, full=1, status="success"):
    with connection() as conn: conn.execute("INSERT INTO maintenance_runs(trigger,started_at,finished_at,status,full) VALUES('startup',?,?,?,?)", (_ago(hours=hours_ago), _ago(hours=hours_ago), status, full))


def test_startup_window_boundaries_and_config():
    assert maintenance.due_for_startup()
    _full_run(21); assert maintenance.due_for_startup()
    _full_run(19); assert not maintenance.due_for_startup()
    _config(full_every_hours=10); _full_run(11)
    with connection() as conn: conn.execute("DELETE FROM maintenance_runs WHERE finished_at>?", (_ago(hours=10, minutes=30),))
    assert maintenance.due_for_startup()
    _full_run(9); assert not maintenance.due_for_startup()


def test_only_unlimited_projectless_real_runs_reset_the_window(tmp_path):
    # Item 24 regression: a --limit run used to suppress the startup run for 20 h.
    engine = FakeEngine()
    for kwargs in ({"limit": 1}, {"project": str(tmp_path)}, {"dry_run": True}):
        assert maintenance.run(engine=engine, **kwargs)["status"] in {"success", "dry_run"} and maintenance.due_for_startup(), kwargs
    _full_run(1, status="error"); assert maintenance.due_for_startup()
    maintenance.run(engine=engine); assert not maintenance.due_for_startup()


def _hook(monkeypatch, capsys, argv, payload, due=True):
    calls = []; monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(maintenance, "due_for_startup", lambda: due)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    monkeypatch.setattr("sys.argv", ["devmem", *argv]); cli.main()
    return capsys.readouterr().out, calls


def test_session_start_briefing_is_identical_and_spawn_is_detached(monkeypatch, capsys, tmp_path):
    _memory("pinned fact", pinned=1); payload = {"cwd": str(tmp_path)}
    due_out, due_calls = _hook(monkeypatch, capsys, ["context", "--hook"], payload, due=True)
    idle_out, idle_calls = _hook(monkeypatch, capsys, ["context", "--hook"], payload, due=False)
    assert due_out == idle_out and "pinned fact" in due_out and idle_calls == []
    (command,), kwargs = due_calls[0]
    assert command[-2:] == ["maintain", "--startup"] and kwargs["start_new_session"] is True and kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"].name == kwargs["stderr"].name == str(cli._maintain_log())


def test_session_end_hook_spawns_detached_run_for_the_project_root(monkeypatch, capsys, tmp_path):
    repo = _repo(tmp_path / "repo"); (repo / "sub").mkdir()
    out, calls = _hook(monkeypatch, capsys, ["maintain", "--session-end", "--hook"], {"cwd": str(repo / "sub"), "transcript_path": "/t.jsonl"})
    (command,), kwargs = calls[0]
    assert out == "" and command[command.index("--project") + 1] == str(repo.resolve()) and command[-2:] == ["--transcript", "/t.jsonl"]
    assert kwargs["start_new_session"] and kwargs["stdin"] is subprocess.DEVNULL and kwargs["stderr"].name == str(cli._maintain_log())


def test_session_end_hook_survives_bad_input_and_logs_it(monkeypatch, capsys, tmp_path):
    # Item 22: bad stdin used to vanish silently.
    monkeypatch.chdir(tmp_path)
    out, calls = _hook(monkeypatch, capsys, ["maintain", "--session-end", "--hook"], "not json")
    assert out == "" and calls and "unreadable hook input" in cli._maintain_log().read_text()


def test_child_engine_sessions_never_start_maintenance(monkeypatch, capsys, tmp_path):
    # Item 9 regression: `claude -p` ran the user's hooks, which queued more maintenance forever.
    monkeypatch.setenv(maintenance.CHILD_ENV, "1"); _memory("pinned fact", pinned=1)
    for argv in (["context", "--hook"], ["maintain", "--session-end", "--hook"]):
        out, calls = _hook(monkeypatch, capsys, argv, {"cwd": str(tmp_path)})
        assert out == "" and calls == []


def test_hook_process_returns_fast_when_disabled(tmp_path):
    _config(enabled=False); started = time.monotonic()
    result = subprocess.run([sys.executable, "-m", "devmem.cli", "maintain", "--session-end", "--hook"], input="not json", capture_output=True, text=True, timeout=10, env=os.environ)
    assert result.returncode == 0 and time.monotonic() - started < 1.0 and not cli._maintain_log().exists()


def test_panel_start_triggers_only_when_due(monkeypatch):
    monkeypatch.setattr("uvicorn.Server.run", lambda self, *a, **k: None); calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: calls.append(a)); monkeypatch.setattr("sys.argv", ["devmem", "panel"])
    for due in (False, True):
        monkeypatch.setattr(maintenance, "due_for_startup", lambda due=due: due); cli.main()
    assert len(calls) == 1 and calls[0][0][-1] == "--startup"
    def broken(): raise RuntimeError("bad schedule")
    monkeypatch.setattr(maintenance, "due_for_startup", broken); cli.main()  # the panel still starts
    assert "bad schedule" in cli._maintain_log().read_text()


def test_cli_normalizes_project_and_fails_on_error(monkeypatch, tmp_path, capsys):
    # Items 12 and 25.
    repo = _repo(tmp_path / "repo"); (repo / "sub").mkdir(); seen = []
    monkeypatch.setattr(maintenance, "run", lambda *a, **k: seen.append(k) or {"status": "error", "checked": 0, "applied": 0, "flagged": 0, "error": "x"})
    monkeypatch.setattr("sys.argv", ["devmem", "maintain", "--project", str(repo / "sub")])
    with pytest.raises(SystemExit) as exited: cli.main()
    assert exited.value.code == 1 and seen[0]["project"] == str(repo.resolve())


def test_session_run_from_subdirectory_finds_project_memories(tmp_path):
    repo = _repo(tmp_path / "repo"); mid = _memory(project=str(repo)); engine = FakeEngine({mid: _proposal(mid)})
    from devmem.db import project_for_cwd
    assert maintenance.run("session_end", project=project_for_cwd(str(repo / "sub")), engine=engine)["applied"] == 1


def test_transcript_reader_with_claude_code_jsonl(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [{"type": "summary", "summary": "s"}, {"type": "user", "message": {"role": "user", "content": "keep this"}},
             {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "private thoughts"}, {"type": "text", "text": "done"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]}},
             {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "tool output"}]}}]
    path.write_text("\n".join(map(json.dumps, lines)) + "\nnot json\n")
    excerpt, note = maintenance.read_transcript(str(path))
    assert excerpt == "transcript:2 keep this\ntranscript:3 done" and note is None
    path.write_text("\n".join(json.dumps({"type": "user", "message": {"role": "user", "content": f"line {n} " + "x" * 90}}) for n in range(1, 1001)))
    excerpt, _ = maintenance.read_transcript(str(path))
    assert len(excerpt.encode()) <= 40_000 and excerpt.endswith("x") and "transcript:1000 line 1000" in excerpt and "transcript:1 line" not in excerpt
    assert all(line.startswith("transcript:") for line in excerpt.splitlines())
    assert maintenance.read_transcript(str(tmp_path / "missing.jsonl")) == (None, "transcript unavailable (FileNotFoundError)")


def test_missing_transcript_is_noted_on_the_run(tmp_path):
    # Item 30.
    result = maintenance.run("session_end", project=str(tmp_path), engine=FakeEngine(), transcript_path=str(tmp_path / "gone.jsonl"))
    assert result["note"] == "transcript unavailable (FileNotFoundError)" and _runs()[-1]["note"] == result["note"]


# ---------------------------------------------------------------- visibility: panel, briefing, MCP

def _client(): from devmem.panel import app as panel; return TestClient(panel.app, base_url="http://127.0.0.1")


def test_failures_show_in_panel_badge_and_briefing():
    # Item 15 regression: error runs were invisible.
    mid = _memory()
    def broken(payload): raise RuntimeError("engine is down")
    maintenance.run(engine=FakeEngine({mid: broken}))
    with connection() as conn: assert "Automatic maintenance failing since" in context_text(conn, None) and "engine is down" in context_text(conn, None)
    page = _client().get("/maintenance").text
    assert "engine is down" in page and "is failing" in page and 'title="Applied changes or open flags"' in _client().get("/audit").text
    with connection() as conn: conn.execute("DELETE FROM maintenance_changes")
    maintenance.run(engine=FakeEngine())
    with connection() as conn: assert "maintenance failing" not in context_text(conn, None)


def test_panel_cards_badge_seen_revert_and_dismiss():
    mid, protected = _memory(), _memory(kind="decision", pinned=1)
    maintenance.run(engine=FakeEngine({mid: _proposal(mid), protected: _proposal(protected)}))
    client = _client()
    badge = 'title="Applied changes or open flags"'
    assert badge in client.get("/audit").text
    page = client.get("/maintenance").text
    assert "branch old is done" in page and "wording: stale status" in page and "Your rule may be outdated" in page
    assert badge not in client.get("/audit").text  # seen (item 27)
    applied, flag = _changes(mid)[-1], _changes(protected)[-1]
    assert client.post(f"/maintenance/{applied['id']}/dismiss", follow_redirects=False).status_code == 404  # item 28
    assert client.post(f"/maintenance/{flag['id']}/dismiss", follow_redirects=False).status_code == 303
    assert client.post(f"/maintenance/{flag['id']}/dismiss", follow_redirects=False).status_code == 404
    assert client.post(f"/maintenance/{applied['id']}/revert", follow_redirects=False).status_code == 303 and _row(mid)["content"] == "branch old, WIP"
    assert client.post(f"/maintenance/{applied['id']}/revert", follow_redirects=False).status_code == 404
    assert client.post("/maintenance/9999/revert", follow_redirects=False).status_code == 404
    other = _memory("second WIP"); maintenance.run(engine=FakeEngine({other: _proposal(other, "second is done")}))
    with connection() as conn: conn.execute("UPDATE memories SET content='user edit',updated_at=? WHERE id=?", (now(), other))
    response = client.post(f"/maintenance/{_changes(other)[-1]['id']}/revert", follow_redirects=False)
    assert response.status_code == 409 and _row(other)["content"] == "user edit"


def test_dry_run_rows_are_not_dismissable_flags():
    mid = _memory(); maintenance.run(dry_run=True, engine=FakeEngine({mid: _proposal(mid)}))
    client = _client(); page = client.get("/maintenance").text
    assert "(dry run)" in page and "/dismiss" not in page and "/revert" not in page
    assert client.post(f"/maintenance/{_changes(mid)[-1]['id']}/dismiss", follow_redirects=False).status_code == 404


def test_maintenance_status_mcp_tool_reports_without_running(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mid = _memory(kind="decision", pinned=1, project=str(tmp_path))
    maintenance.run(engine=FakeEngine({mid: _proposal(mid)})); maintenance.run(dry_run=True, engine=FakeEngine())
    with connection() as conn: conn.execute("INSERT INTO maintenance_runs(trigger,started_at,status) VALUES('startup',?,'requested')", (now(),))
    monkeypatch.setattr(maintenance, "run", lambda *a, **k: pytest.fail("maintenance_status must not run maintenance"))
    status = mcp_server.maintenance_status()
    assert status["last_run"]["status"] == "dry_run" and [f["memory_id"] for f in status["pending_flags"]] == [mid] and status["failing"] is None


def test_v6_database_migrates_with_backup(tmp_path, monkeypatch):
    path = tmp_path / "old.db"; conn = connect(path)
    conn.execute("INSERT INTO memories(kind,title,content,created_at,updated_at) VALUES('note','kept','body',?,?)", (now(), now()))
    conn.execute("DROP TABLE maintenance_changes"); conn.execute("DROP TABLE maintenance_runs"); conn.execute("UPDATE schema_version SET version=6"); conn.commit(); conn.close()
    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 13 and migrated.execute("SELECT title FROM memories").fetchone()[0] == "kept"
    assert {"full", "options", "note"} <= {r[1] for r in migrated.execute("PRAGMA table_info(maintenance_runs)")}
    backup = sqlite3.connect(next((tmp_path / "backups").glob("devmem-before-v13-from-v6-*.db")))
    assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == 6 and backup.execute("SELECT title FROM memories").fetchone()[0] == "kept"


def test_v7_database_gains_v8_columns_and_keeps_rows(tmp_path):
    path = tmp_path / "v7.db"; conn = connect(path)
    conn.execute("DROP TABLE maintenance_changes"); conn.execute("DROP TABLE maintenance_runs")
    for statement in _V7_STATEMENTS: conn.execute(statement)
    conn.execute("INSERT INTO maintenance_runs(trigger,started_at,status) VALUES('manual',?,'success')", (now(),)); conn.execute("UPDATE schema_version SET version=7"); conn.commit(); conn.close()
    migrated = connect(path)
    assert tuple(migrated.execute("SELECT status,full FROM maintenance_runs").fetchone()) == ("success", 0)
    assert {"old_review_after", "new_review_after", "seen_at"} <= {r[1] for r in migrated.execute("PRAGMA table_info(maintenance_changes)")}
    assert list((tmp_path / "backups").glob("devmem-before-v13-from-v7-*.db"))


# ---------------------------------------------------------------- cleanup

def _cleanup_hash():
    with connection() as conn:
        tables = ("memories", "memory_links", "memory_revisions", "maintenance_runs", "maintenance_changes")
        return hashlib.sha256(json.dumps([[list(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in tables]).encode()).hexdigest()


def _cleanup_run(status="success", age=2):
    with connection() as conn:
        stamp = _ago(days=age)
        return conn.execute("INSERT INTO maintenance_runs(trigger,started_at,finished_at,status) VALUES('manual',?,?,?)", (stamp, stamp, status)).lastrowid


def _cleanup_change(run, memory_id, *, evidence="[]", blocked=None, applied=0, reverted=None, age=2):
    with connection() as conn:
        return conn.execute("""INSERT INTO maintenance_changes(run_id,memory_id,action,applied,summary,evidence,blocked_by,reverted_at,created_at)
            VALUES(?,?,'flag',?,'flag',?,?,?,?)""", (run, memory_id, applied, evidence, blocked, reverted, _ago(days=age))).lastrowid


def test_cleanup_closes_deleted_superseded_edited_and_resolved_flags_but_keeps_guarded_flags():
    run = _cleanup_run()
    deleted = _cleanup_change(run, 9001)
    superseded, edited, resolved = _memory("old"), _memory("old"), _memory("plain")
    newer = _memory("new")
    _link(newer, superseded, "supersedes")
    sup = _cleanup_change(run, superseded)
    edit = _cleanup_change(run, edited)
    with connection() as conn: conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (now(), edited))
    evidence = json.dumps([{"ref": "gone.py", "check": "path", "result": "missing"}])
    evidence_change = _cleanup_change(run, resolved, evidence=evidence)
    guarded = [_cleanup_change(run, _memory("plain"), blocked=blocked) for blocked in ("protected", "secret", "engine_error")]
    result = maintenance.cleanup()
    assert result["closed_flags"] == 4
    with connection() as conn:
        closed = {row["id"]: row for row in conn.execute("SELECT id,reverted_at,summary FROM maintenance_changes")}
    for change in (deleted, sup, edit, evidence_change): assert closed[change]["reverted_at"] and "closed:" in closed[change]["summary"]
    assert all(closed[change]["reverted_at"] is None for change in guarded)


def test_cleanup_keeps_evidence_it_cannot_recheck_and_refreshes_path_checks(tmp_path, monkeypatch):
    run = _cleanup_run()
    audit = _cleanup_change(run, _memory("plain"), evidence=json.dumps([{"ref": "audit", "check": "audit", "result": "old"}]))
    model = _cleanup_change(run, _memory("plain"), evidence=json.dumps([{"ref": "model", "detail": "uncertain"}]))
    relative = _cleanup_change(run, _memory("plain"), evidence=json.dumps([{"ref": "yesterday", "check": "relative-date", "result": "words present"}]))
    repo = _repo(tmp_path / "repo"); (repo / "gone.txt").write_text("x"); _git(repo, "add", "gone.txt"); _git(repo, "commit", "-q", "-m", "add")
    mid = _memory("see gone.txt", project=str(repo))
    maintenance._evidence(_row(mid))
    (repo / "gone.txt").unlink()
    monkeypatch.setattr(maintenance, "_git", lambda *args: (_ for _ in ()).throw(OSError("git unavailable")))
    path = _cleanup_change(run, mid, evidence=json.dumps([{"ref": "gone.txt", "check": "path", "result": "missing"}]))
    maintenance.cleanup()
    with connection() as conn:
        open_flags = {row["id"] for row in conn.execute("SELECT id FROM maintenance_changes WHERE reverted_at IS NULL")}
    assert {audit, model, relative, path} <= open_flags


def test_ingest_loop_continues_when_message_pruning_fails(monkeypatch):
    from devmem.panel import app as panel
    calls = []
    monkeypatch.setattr(maintenance, "prune_messages", lambda: (_ for _ in ()).throw(RuntimeError("broken prune")))
    monkeypatch.setattr(panel, "ingest_all", lambda: calls.append("ingest"))
    monkeypatch.setattr(panel, "ensure_embeddings", lambda conn: calls.append("embeddings"))
    panel._ingest_loop(SimpleNamespace(wait=lambda seconds: True))
    assert calls == ["ingest", "embeddings"]


def test_cleanup_prunes_old_dry_run_runs_and_their_changes():
    run = _cleanup_run("dry_run")
    _cleanup_change(run, _memory("plain"))
    assert maintenance.cleanup()["dry_run_runs"] == 1
    with connection() as conn: assert conn.execute("SELECT COUNT(*) FROM maintenance_runs WHERE id=?", (run,)).fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM maintenance_changes WHERE run_id=?", (run,)).fetchone()[0] == 0


def test_cleanup_prunes_old_bookkeeping_and_superseded_error_runs():
    requested, done, skipped = (_cleanup_run(status) for status in ("requested", "done", "skipped"))
    error = _cleanup_run("error", age=8)
    _cleanup_run("success", age=1)
    result = maintenance.cleanup()
    assert result["bookkeeping_runs"] == 3 and result["error_runs"] == 1
    with connection() as conn: assert conn.execute("SELECT COUNT(*) FROM maintenance_runs WHERE id IN (?,?,?,?)", (requested, done, skipped, error)).fetchone()[0] == 0


def test_cleanup_prunes_closed_rows_never_an_unreverted_applied_change():
    run = _cleanup_run()
    old_applied = _cleanup_change(run, _memory("plain"), applied=1, age=31)
    dismissed = _cleanup_change(run, _memory("plain"), reverted=_ago(days=31), age=31)
    no_change = _cleanup_change(run, _memory("plain"), blocked="no_change", age=31)
    assert maintenance.cleanup()["closed_rows"] == 2
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM maintenance_changes WHERE id=?", (old_applied,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM maintenance_changes WHERE id IN (?,?)", (dismissed, no_change)).fetchone()[0] == 0


def test_cleanup_dry_run_is_read_only_and_lock_and_panel_notice_work():
    run = _cleanup_run("dry_run"); _cleanup_change(run, _memory("plain"))
    before = _cleanup_hash(); result = maintenance.cleanup(dry_run=True)
    assert result["status"] == "dry_run" and _cleanup_hash() == before
    with maintenance.Lock(maintenance.lock_path()) as held:
        assert held.acquired and maintenance.cleanup() == {"status": "locked"}
    client = _client()
    response = client.post("/maintenance/cleanup", headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    assert "deleted 1 dry-run runs" in client.get(response.headers["location"]).text
