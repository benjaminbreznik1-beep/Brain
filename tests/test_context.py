import io
import json
import os
import subprocess
import sys

from devmem import cli, db, mcp_server
from devmem.db import connect, connection, context_text


def _insert(conn, project, kind, title, content):
    conn.execute(
        "INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at) VALUES(?,?,?,?,?,'x','x')",
        (project, kind, title, content, ""),
    )


def _run_cli(monkeypatch, capsys, argv, stdin=""):
    monkeypatch.setattr(sys, "argv", ["devmem", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    cli.main()
    return capsys.readouterr()


def test_context_text_matches_mcp_project_context(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("DEVMEM_DB", str(db_file))
    with connection() as conn:
        _insert(conn, str(project), "decision", "Project choice", "Use SQLite")
        _insert(conn, None, "preference", "Global rule", "Keep replies concise")
    with connect(db_file) as conn:
        expected = context_text(conn, str(project))

    monkeypatch.chdir(project)
    monkeypatch.setattr(mcp_server, "_client_id", None)
    monkeypatch.setattr(mcp_server, "_client_name", None)
    assert mcp_server.project_context() == expected


def test_context_hook_emits_session_start_payload_without_embedder(tmp_path, monkeypatch, capsys):
    db_file = tmp_path / "memory.db"
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("DEVMEM_DB", str(db_file))
    with connection() as conn:
        _insert(conn, str(project), "note", "Project memory", "Remember this project detail")
        _insert(conn, None, "preference", "Global preference", "Always use tests")

    def fail_embedder():
        raise AssertionError("the context command must not load the embedder")

    monkeypatch.setattr(db, "get_embedder", fail_embedder)
    result = _run_cli(monkeypatch, capsys, ["context", "--hook"], json.dumps({"cwd": str(project)}))
    payload = json.loads(result.out)
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    assert output["additionalContext"].startswith("devmem memory (auto-loaded at session start; use memory_search / memory_get for more):\n")
    assert "Global preference" in output["additionalContext"]
    assert "Project memory" in output["additionalContext"]


def test_context_hook_is_quiet_for_garbage_stdin_missing_or_empty_db(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("DEVMEM_DB", str(missing))
    result = _run_cli(monkeypatch, capsys, ["context", "--hook"], "not json")
    assert result.out == ""

    empty = tmp_path / "empty.db"
    connect(empty).close()
    monkeypatch.setenv("DEVMEM_DB", str(empty))
    result = _run_cli(monkeypatch, capsys, ["context", "--hook"], "not json")
    assert result.out == ""


def test_context_hook_handles_non_sqlite_missing_db_and_non_mapping_payloads(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    garbage = tmp_path / "garbage.db"
    garbage.write_text("not sqlite")
    monkeypatch.setenv("DEVMEM_DB", str(garbage))
    result = _run_cli(monkeypatch, capsys, ["context", "--hook"], "[]")
    assert result.out == ""

    missing = tmp_path / "does-not-exist.db"
    monkeypatch.setenv("DEVMEM_DB", str(missing))
    result = _run_cli(monkeypatch, capsys, ["context", "--hook"], '{"cwd": 5}')
    assert result.out == "" and not missing.exists()

    project = tmp_path / "project"; (project / ".git").mkdir(parents=True)
    db_file = tmp_path / "memory.db"
    monkeypatch.setenv("DEVMEM_DB", str(db_file))
    with connection() as conn: _insert(conn, str(project), "note", "Fallback cwd", "works")
    monkeypatch.chdir(project)
    for payload in ("[]", '{"cwd": 5}'):
        result = _run_cli(monkeypatch, capsys, ["context", "--hook"], payload)
        assert "Fallback cwd" in result.out


def test_context_hook_subprocess_never_imports_fastembed(tmp_path):
    db_file = tmp_path / "garbage.db"; db_file.write_text("not sqlite")
    script = '''
import builtins, sys
from devmem import cli
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "fastembed" or name.startswith("fastembed."):
        raise AssertionError("fastembed imported")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.argv = ["devmem", "context", "--hook"]
cli.main()
'''
    env = {**os.environ, "DEVMEM_DB": str(db_file), "DEVMEM_EMBED": "1"}
    result = subprocess.run([sys.executable, "-c", script], input="[]", text=True, capture_output=True, env=env, cwd=tmp_path)
    assert result.returncode == 0 and result.stdout == ""
