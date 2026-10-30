"""Rules guard behavior; these tests use the temp DB fixture."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from devmem import cli, guard, maintenance, mcp_server
from devmem.hook_log import guard_log
from devmem.db import connection, save_memory


def _memory(content, title="Rule", project=None):
    with connection() as conn: return save_memory(conn, content=content, title=title, project=project)


def _data(cwd="/work/demo"):
    with connection() as conn: return guard.rules_data(conn, cwd)


def test_parse_valid_and_invalid_lines():
    rules, invalid, notices = guard.parse_rules('GUARD: {"tool":"Bash","command":"git commit*","action":"ask"}\nGUARD: nope\nGUARD: {"tool":"Bash","action":"block"}\nGUARD: {"tool":"Bash","action":"ask","extra":1}')
    assert rules == [{"tool": "Bash", "command": "git commit*", "action": "ask"}]
    assert [item["reason"] for item in invalid] == ["invalid JSON", "action must be deny or ask", "unknown field extra"]
    assert notices == []


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


def test_review_prefixes_relative_paths_quotes_and_notices(tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    _memory('GUARD: {"tool":"Bash","command":"git commit*","action":"deny"}\n'
            'GUARD: {"tool":"Read","path":"src/**/conf.py","action":"deny"}\n'
            'guard: {"tool":"Read","action":"ask"}\n'
            'GUARD: {"tool":"bash","action":"ask"}\n'
            'GUARD: {"tool":"WebFetch","path":"","action":"ask"}', project=str(root))
    data = _data(str(root))
    for command in ("then env X=1 sudo -u root time nohup command exec timeout 3 nice -n 1 git  commit -m x", "cat '.env' # don't"):
        if command.startswith("then"):
            assert guard.evaluate(data, {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(root)})[0] == "deny"
    for path in (str(root / "src" / "conf.py"), str(root / "src" / "a" / "conf.py")):
        assert guard.evaluate(data, {"tool_name": "Read", "tool_input": {"file_path": path}, "cwd": str(root)})[0] == "deny"
    assert {item["reason"] for item in data["invalid"]} == {"GUARD: must start the line, in capitals", "command and path must be non-empty"}
    assert {item["reason"] for item in data["notices"]} == {"tool bash is not a known Claude tool (matching is case-sensitive)", "did you mean Bash?"}


def test_hook_is_quiet_on_no_match_broken_input_and_missing_db(monkeypatch, capsys, tmp_path):
    _memory('GUARD: {"tool":"Read","path":"**/.env*","action":"deny"}')
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name":"Read","tool_input":{"file_path":"ok.py"},"cwd":"/work"}'))
    guard.hook(); assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("{")); guard.hook(); assert capsys.readouterr().out == ""
    guard_log = tmp_path / "guard.log"
    assert len(guard_log.read_text().splitlines()) == 1
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "missing.db")); monkeypatch.setattr(sys, "stdin", io.StringIO("{}")); guard.hook(); assert capsys.readouterr().out == ""
    assert len(guard_log.read_text().splitlines()) == 2


def test_hook_exact_output_inprocess_and_subprocess(monkeypatch, capsys, tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    deny = _memory('GUARD: {"tool":"Bash","command":"git commit*","action":"deny"}', "No commit", str(root))
    ask = _memory('GUARD: {"tool":"Bash","command":"git *","action":"ask"}', "Ask git", str(root))
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(root)}
    expected = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"devmem rule #{deny}: No commit"}}, separators=(",", ":"))
    monkeypatch.setattr(sys, "argv", ["devmem", "guard", "--hook"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    cli.main()
    assert capsys.readouterr().out == expected + "\n"
    env = {key: value for key, value in os.environ.items() if key.startswith("DEVMEM_") or key in {"PATH", "HOME"}}
    run = subprocess.run([sys.executable, "-m", "devmem.cli", "guard", "--hook"], input=json.dumps(payload), text=True,
                         capture_output=True, env=env, cwd=str(tmp_path))
    assert run.returncode == 0 and run.stdout == expected + "\n" and run.stderr == ""
    assert f"#{ask}" not in run.stdout


def test_hook_joins_same_action_winners_and_never_emits_allow(monkeypatch, capsys):
    one = _memory('GUARD: {"tool":"Read","action":"ask"}', "One")
    two = _memory('GUARD: {"tool":"Read","path":"*.txt","action":"ask"}', "Two")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name":"Read","tool_input":{"file_path":"a.txt"}}'))
    guard.hook()
    assert capsys.readouterr().out == json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
        "permissionDecisionReason": f"devmem rule #{one}: One; #{two}: Two"}}, separators=(",", ":")) + "\n"


def test_guard_log_is_rate_limited_and_context_hook_reports_startup_errors(monkeypatch, capsys, tmp_path):
    guard_log("same failure", db_path="test.db", tool_name="Read", cwd="/work")
    guard_log("same failure", db_path="test.db", tool_name="Read", cwd="/work")
    line = (tmp_path / "guard.log").read_text()
    assert line.count("same failure") == 1 and "db=test.db tool=Read cwd=/work" in line
    hook_error = tmp_path / "guard-hook.err"; hook_error.write_text("failed\n")
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "absent.db"))
    monkeypatch.setattr(sys, "argv", ["devmem", "context", "--hook"])
    cli.main()
    assert f"devmem guard: startup errors logged in {hook_error} (rules may not be enforced)" in capsys.readouterr().out


def test_guard_log_rotates_and_readonly_rules_only_returns_guard_memories(tmp_path):
    log_path = tmp_path / "guard.log"
    log_path.write_bytes(b"x" * 1_000_001)
    guard_log("fresh failure", db_path="test.db", tool_name="Read", cwd="/work")
    assert (tmp_path / "guard.log.1").is_file() and "fresh failure" in log_path.read_text()
    _memory("ordinary non-rule memory", "Plain")
    rule = _memory('GUARD: {"tool":"Read","action":"ask"}', "Rule")
    assert [item["id"] for item in guard.readonly_rules()["rules"]] == [rule]


def test_all_bash_prefixes_quotes_and_rule_isolation(monkeypatch, tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    _memory('GUARD: {"tool":"Bash","command":"git commit*","action":"deny"}\nGUARD: {"tool":"Bash","path":".env","action":"ask"}', project=str(root))
    data = _data(str(root))
    for prefix in ("then", "do", "else", "elif", "!", "env A=1 -i", "time", "nohup", "sudo -u root", "command", "exec", "timeout --foreground 5", "nice -n 2"):
        assert guard.evaluate(data, {"tool_name": "Bash", "tool_input": {"command": prefix + " git  commit -m x"}, "cwd": str(root)})[0] == "deny"
    assert guard.evaluate(data, {"tool_name": "Bash", "tool_input": {"command": "cat '.env' # don't"}, "cwd": str(root)})[0] == "ask"
    bad, good = data["rules"]
    def raises(rule, *args):
        if rule is bad: raise RuntimeError("broken rule")
        return True
    monkeypatch.setattr(guard, "matches", raises)
    action, matched = guard.evaluate(data, {"tool_name": "Bash", "tool_input": {}, "cwd": str(root)})
    assert action == "ask" and matched == [good]
    assert "broken rule" in (tmp_path / "guard.log").read_text()


def test_rule_validation_and_scope_transparency(tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    rules, invalid, notices = guard.parse_rules('\n GUARD: {"tool":"Read","action":"ask"}\nGUARD: {"tool":"Agent","action":"ask"}\nGUARD: {"tool":"mcp__x","action":"ask"}\nGUARD: {"tool":"*","action":"ask"}\nGUARD: {"tool":"WebFetch","path":"","action":"ask"}\nGUARD: {"tool":"webfetch","action":"ask"}')
    assert [rule["tool"] for rule in rules] == ["Agent", "mcp__x", "*", "webfetch"]
    assert [item["reason"] for item in invalid] == ["GUARD: must start the line, in capitals", "command and path must be non-empty"]
    assert [item["reason"] for item in notices] == ["tool webfetch is not a known Claude tool (matching is case-sensitive)", "did you mean WebFetch?"]
    _memory('GUARD: {"tool":"Read","action":"ask"}', project=str(root))
    with connection() as conn:
        assert guard.rules_data(conn, str(root))["project"] == str(root)


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
    updated = mcp_server.memory_update(saved["id"], content='GUARD: {"tool":"Read","path":"","action":"ask"}')
    assert updated["warnings"] and mcp_server.memory_update(saved["id"], content=None).get("warnings") is None
    assert maintenance._protected({"kind": "note", "tags": "", "content": "GUARD: broken"}, {"protected_kinds": []})


def test_rules_cli_check_scopes_and_invalid_codex_exit(monkeypatch, capsys, tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    _memory('GUARD: {"tool":"Read","path":"secret","action":"deny"}', "Project", str(root))
    _memory('GUARD: broken', "Broken", str(root))
    monkeypatch.setattr(sys, "argv", ["devmem", "rules", "--check", json.dumps({"tool_name": "Read", "tool_input": {"file_path": "secret"}, "cwd": str(root)}), "--cwd", "/other"])
    cli.main()
    assert capsys.readouterr().out.startswith("deny ")
    monkeypatch.setattr(sys, "argv", ["devmem", "rules", "--check", "[]"])
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    assert "JSON object" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["devmem", "rules", "--cwd", str(root)])
    cli.main(); plain = capsys.readouterr().out
    assert f"scope: global + {root}" in plain and "invalid GUARD" in plain
    monkeypatch.setattr(sys, "argv", ["devmem", "rules", "--codex", "--cwd", str(root)])
    with pytest.raises(SystemExit, match="2"):
        cli.main()
    assert "invalid GUARD" in capsys.readouterr().err


def test_cli_maintenance_and_read_only_command_smoke(monkeypatch, capsys):
    for argv in (["audit"], ["search", "nothing"], ["maintenance-cleanup", "--dry-run"]):
        monkeypatch.setattr(sys, "argv", ["devmem", *argv])
        cli.main()
        capsys.readouterr()
    monkeypatch.setattr(maintenance, "run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("maintenance broke")))
    monkeypatch.setattr(sys, "argv", ["devmem", "maintain"])
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    assert "devmem maintain failed: RuntimeError: maintenance broke" in capsys.readouterr().err


def test_guard_import_does_not_import_numpy():
    code = "import sys; import devmem.guard; assert 'numpy' not in sys.modules"
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cli_hook_does_not_import_numpy():
    code = "import io,sys; import devmem.cli; sys.argv=['devmem','guard','--hook']; sys.stdin=io.StringIO('{}'); devmem.cli.main(); assert 'numpy' not in sys.modules"
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
