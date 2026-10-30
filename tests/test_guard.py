"""Rules guard behavior; these tests deliberately use the temp DB fixture."""
from __future__ import annotations

import io
import json
import subprocess
import sys

from devmem import cli, guard, maintenance, mcp_server
from devmem.db import connection, save_memory


def _memory(content, title="Rule", project=None):
    with connection() as conn: return save_memory(conn, content=content, title=title, project=project)


def _data(cwd="/work/demo"):
    with connection() as conn: return guard.rules_data(conn, cwd)


def test_parse_valid_and_invalid_lines():
    rules, invalid = guard.parse_rules('GUARD: {"tool":"Bash","command":"git commit*","action":"ask"}\nGUARD: nope\nGUARD: {"tool":"Bash","action":"block"}\nGUARD: {"tool":"Bash","action":"ask","extra":1}')
    assert rules == [{"tool": "Bash", "command": "git commit*", "action": "ask"}]
    assert [item["reason"] for item in invalid] == ["invalid JSON", "action must be deny or ask", "unknown field extra"]


def test_scope_superseded_and_deny_beats_ask(tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    child = root / "child"; child.mkdir()
    global_id = _memory('GUARD: {"tool":"Bash","action":"ask"}', "Global")
    project_id = _memory('GUARD: {"tool":"Bash","command":"git *","action":"deny"}', "Project", str(root))
    _memory('GUARD: {"tool":"Bash","action":"deny"}', "Other", "/other")
    old = _memory('GUARD: {"tool":"Read","action":"deny"}', "Old", str(root)); newer = _memory("replacement", "New", str(root))
    with connection() as conn: conn.execute("INSERT INTO memory_links(from_id,to_id,type,note,created_at) VALUES(?,?,?,?,?)", (newer, old, "supersedes", "", "2026-01-01T00:00:00+00:00"))
    action, matched = guard.evaluate(_data(str(child)), {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": str(child)})
    assert action == "deny" and [rule["id"] for rule in matched] == [project_id]
    assert global_id in [rule["id"] for rule in _data(str(child))["rules"]]
    assert old not in [rule["id"] for rule in _data(str(child))["rules"]]


def test_bash_segments_paths_and_no_match(tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    _memory('GUARD: {"tool":"Bash","command":"git commit*","action":"deny"}\nGUARD: {"tool":"*","path":"**/.env*","action":"ask"}', project=str(root))
    data = _data(str(root))
    assert guard.evaluate(data, {"tool_name": "Bash", "tool_input": {"command": "cd x && git commit -m y"}, "cwd": str(root)})[0] == "deny"
    assert guard.evaluate(data, {"tool_name": "Bash", "tool_input": {"command": "FOO=1 git commit -m y"}, "cwd": str(root)})[0] == "deny"
    for value in (".env", "dir/.env.local", str(root / ".env")):
        assert guard.evaluate(data, {"tool_name": "Read", "tool_input": {"file_path": value}, "cwd": str(root)})[0] == "ask"
    for command in ("cat .env", "cat '.env'", 'rm -f "../x/.env.local"', "wc<.env"):
        assert guard.evaluate(data, {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(root)})[0] == "ask", command
    assert guard.evaluate(data, {"tool_name": "Read", "tool_input": {"file_path": "ok.py"}, "cwd": str(root)}) == ("none", [])


def test_review_bypasses_are_caught(tmp_path, monkeypatch):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    _memory('GUARD: {"tool":"Bash","command":"git commit*","action":"deny"}\n'
            'GUARD: {"tool":"*","path":"**/id_rsa","action":"deny"}\n'
            'GUARD: {"tool":"*","path":"src/**/conf.py","action":"deny"}\n'
            f'GUARD: {{"tool":"*","path":"{tmp_path}/secret/*","action":"deny"}}', project=str(root))
    data = _data(str(root))
    bash = lambda command: guard.evaluate(data, {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(root)})[0]
    read = lambda path: guard.evaluate(data, {"tool_name": "Read", "tool_input": {"file_path": path}, "cwd": str(root)})[0]
    for command in ('GIT_AUTHOR_NAME="A B" git commit -m x', "FOO= git commit", "sleep 1 & git commit -m x", "(git commit -m x)", "echo $(git commit)",
                    "cat id_rsa;ls", "cat id_rsa|base64", "cat id_rsa&&x", "cat src/conf.py", "cat ~/secret/a", "cat $HOME/secret/a", "x --file=id_rsa"):
        assert bash(command) == "deny", command
    assert read("src/conf.py") == read("src/a/b/conf.py") == read("~/secret/a") == "deny"
    assert bash("git status") == read("src/other.py") == "none"


def test_hook_is_quiet_on_no_match_broken_input_and_missing_db(monkeypatch, capsys, tmp_path):
    _memory('GUARD: {"tool":"Read","path":"**/.env*","action":"deny"}')
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name":"Read","tool_input":{"file_path":"ok.py"},"cwd":"/work"}'))
    guard.hook(); assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("{")); guard.hook(); assert capsys.readouterr().out == ""
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "missing.db")); monkeypatch.setattr(sys, "stdin", io.StringIO("{}")); guard.hook(); assert capsys.readouterr().out == ""


def test_rules_codex_mcp_warning_and_protection(monkeypatch, capsys, tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    _memory('GUARD: {"tool":"Bash","command":"git commit*","action":"deny"}', "No commits", str(root))
    monkeypatch.setattr(sys, "argv", ["devmem", "rules", "--codex", "--cwd", str(root)])
    _memory('GUARD: {"tool":"*","path":"**/.env*","action":"ask"}', "Env files", str(root))
    cli.main(); out = capsys.readouterr().out
    assert "- NEVER run Bash commands matching `git commit*` (devmem" in out and "without asking" not in out
    assert "- ASK the user before using any tool on paths matching `**/.env*` (devmem" in out
    assert guard.parse_rules('GUARD: {"tool":"WebFetch","action":"ask"}')[0]
    result = mcp_server.rules_list(str(root)); assert result["rules"][0]["title"] == "No commits"
    saved = mcp_server.memory_save("GUARD: broken", scope="global"); assert saved["warnings"]
    assert maintenance._protected({"kind": "note", "tags": "", "content": "GUARD: broken"}, {"protected_kinds": []})


def test_guard_import_does_not_import_numpy():
    code = "import sys; import devmem.guard; assert 'numpy' not in sys.modules"
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
