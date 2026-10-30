"""Fast, stdlib-only rule guard for Claude PreToolUse hooks."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sqlite3
import sys
from pathlib import Path

from .hook_log import _log
from .projects import project_for_cwd

_FIELDS = {"tool", "command", "path", "action"}
_ASSIGNMENT = re.compile(r"""^(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S*)\s+)+""")
_SEGMENT = re.compile(r"&&|\|\||;|\||&|\n|\$\(|[()`{}]")
_TOKEN_SPLIT = re.compile(r"[\s<>;|&()`]+")
_TOOL = re.compile(r"\*|[A-Za-z][A-Za-z0-9_-]*")


def parse_rules(content: str) -> tuple[list[dict], list[dict]]:
    """Return valid GUARD rules and invalid-line reports from one memory."""
    rules, invalid = [], []
    for line in content.splitlines():
        if not line.startswith("GUARD:"):
            continue
        text = line.removeprefix("GUARD:").strip()
        try:
            rule = json.loads(text)
        except json.JSONDecodeError:
            invalid.append({"line": line, "reason": "invalid JSON"}); continue
        if not isinstance(rule, dict):
            invalid.append({"line": line, "reason": "must be a JSON object"}); continue
        unknown = set(rule) - _FIELDS
        if unknown:
            invalid.append({"line": line, "reason": "unknown field " + sorted(unknown)[0]}); continue
        if not isinstance(rule.get("tool"), str) or not _TOOL.fullmatch(rule["tool"]):
            invalid.append({"line": line, "reason": "unknown tool"}); continue
        if rule.get("action") not in ("deny", "ask"):
            invalid.append({"line": line, "reason": "action must be deny or ask"}); continue
        if any(key in rule and not isinstance(rule[key], str) for key in ("command", "path")):
            invalid.append({"line": line, "reason": "command and path must be strings"}); continue
        if "command" in rule and rule["tool"] != "Bash":
            invalid.append({"line": line, "reason": "command is only valid for Bash"}); continue
        rules.append(rule)
    return rules, invalid


def _rows(conn: sqlite3.Connection, cwd: str | None) -> list[sqlite3.Row]:
    project = project_for_cwd(cwd)
    return conn.execute("""SELECT m.id,m.title,m.content,m.project FROM memories m
        WHERE (m.project IS NULL OR m.project=?) AND NOT EXISTS
        (SELECT 1 FROM memory_links l WHERE l.to_id=m.id AND l.type='supersedes') ORDER BY m.id""", (project,)).fetchall()


def rules_data(conn: sqlite3.Connection, cwd: str | None = None) -> dict:
    """Return active scope rules and malformed GUARD lines."""
    rules, invalid = [], []
    for row in _rows(conn, cwd):
        parsed, bad = parse_rules(row["content"])
        rules.extend({**rule, "id": row["id"], "title": row["title"]} for rule in parsed)
        invalid.extend({**item, "id": row["id"], "title": row["title"]} for item in bad)
    return {"rules": rules, "invalid": invalid}


def warnings(content: str) -> list[str]:
    """Human-readable warnings for malformed GUARD lines without rejecting a memory."""
    return [f"{item['line']}: {item['reason']}" for item in parse_rules(content)[1]]


def _glob_variants(pattern: str) -> list[str]:
    """Each `**/` may also match zero directories: try every combination of dropping them."""
    parts = pattern.split("**/")
    if len(parts) > 7: return [pattern, pattern.replace("**/", "")]
    variants = [parts[0]]
    for part in parts[1:]: variants = [v + glue + part for v in variants for glue in ("**/", "")]
    return variants


def _glob(pattern: str, value: str) -> bool:
    return any(fnmatch.fnmatchcase(value, variant) for variant in _glob_variants(pattern))


def _paths(value: str, cwd: str | None) -> tuple[str, str]:
    base = cwd or os.getcwd()
    return value, os.path.abspath(os.path.join(base, os.path.expandvars(os.path.expanduser(value))))


def _path_matches(pattern: str, value: str, cwd: str | None) -> bool:
    return any(_glob(pattern, path) for path in _paths(value, cwd))


def _bash_commands(command: str) -> list[str]:
    pieces = [command]
    pieces.extend(part.strip() for part in _SEGMENT.split(command) if part.strip())
    return pieces + [_ASSIGNMENT.sub("", part).strip() for part in pieces]


def _tokens(command: str) -> list[str]:
    """Raw tokens plus shell-unquoted ones, so `cat '.env'`, `cat<.env` and `--file=.env` are seen too."""
    raw = _TOKEN_SPLIT.split(command)
    try: unquoted = shlex.split(_TOKEN_SPLIT.sub(" ", command))
    except ValueError: unquoted = []
    tokens = [token for token in raw + unquoted if token]
    return tokens + [token.rsplit("=", 1)[1] for token in tokens if "=" in token and not token.endswith("=")]


def matches(rule: dict, tool_name: str, tool_input: dict, cwd: str | None) -> bool:
    if rule["tool"] not in ("*", tool_name):
        return False
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if "command" in rule:
        if tool_name != "Bash" or not isinstance(command, str) or not any(_glob(rule["command"], item) for item in _bash_commands(command)):
            return False
    path = next((tool_input[key] for key in ("file_path", "notebook_path", "path") if isinstance(tool_input.get(key), str)), None) if isinstance(tool_input, dict) else None
    if "path" in rule:
        path_match = isinstance(path, str) and _path_matches(rule["path"], path, cwd)
        token_match = tool_name == "Bash" and isinstance(command, str) and any(_path_matches(rule["path"], token, cwd) for token in _tokens(command))
        if not path_match and not token_match:
            return False
    return True


def evaluate(data: dict, payload: dict) -> tuple[str, list[dict]]:
    """Return deny, ask, or none and all rules at the winning level."""
    tool_name, tool_input, cwd = payload.get("tool_name"), payload.get("tool_input"), payload.get("cwd")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return "none", []
    matched = [rule for rule in data["rules"] if matches(rule, tool_name, tool_input, cwd if isinstance(cwd, str) else None)]
    action = "deny" if any(rule["action"] == "deny" for rule in matched) else "ask" if matched else "none"
    return action, [rule for rule in matched if rule["action"] == action]


def readonly_rules(cwd: str | None = None) -> dict:
    path = Path(os.environ.get("DEVMEM_DB", "~/.devmem/devmem.db")).expanduser().resolve()
    uri = path.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return rules_data(conn, cwd)


def hook() -> None:
    """Run quietly and fail open for Claude's PreToolUse hook."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input is not an object")
        action, matched = evaluate(readonly_rules(payload.get("cwd")), payload)
        if action == "none":
            return
        reason = "; ".join(f"#{rule['id']}: {rule['title']}" for rule in matched)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": action,
                                                  "permissionDecisionReason": "devmem rule " + reason}}, separators=(",", ":")))
    except Exception as exc:
        _log(f"guard hook failed: {type(exc).__name__}: {exc}")
