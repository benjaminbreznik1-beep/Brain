from __future__ import annotations

import argparse
import json
import os
import re
import sys

from .db import audit_memories, context_text, connection, db_path, ensure_embeddings, fts_query, hybrid_search, project_for_cwd, readonly_connection
from .embed import get_embedder


_NO_CONTEXT = "No saved project context."
_HOOK_HEADER = "devmem memory (auto-loaded at session start; use memory_search / memory_get for more):"
_REMINDER_RULES = (
    (r"\bgit\s+(switch\s+-c|checkout\s+-b)\b", "New branch started: if this belongs to a tracked task, update its status line in memory (the pinned board and the task's own memory)."),
    (r"\bgit\s+commit\b", "Commit made: update the task's status in memory (pinned board + task memory) if its phase changed."),
    (r"\bgit\s+merge\b", "Merge done: mark the task's phase in memory (pinned board + task memory)."),
    (r"\bcodex\s+exec\b", "Work dispatched to Codex: record 'dispatched' in the task's memory now and update it again when the result comes back."),
)


def _hook_cwd() -> str:
    try:
        payload = json.load(sys.stdin)
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        return cwd if isinstance(cwd, str) else os.getcwd()
    except Exception:
        return os.getcwd()


def _context_for_cwd(cwd: str) -> str:
    if not db_path().is_file():
        return _NO_CONTEXT
    with readonly_connection() as conn:
        return context_text(conn, project_for_cwd(cwd))


def _reminder_rules():
    path = os.environ.get("DEVMEM_REMINDERS", "~/.devmem/reminders.json")
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _REMINDER_RULES
    except Exception as exc:
        print(f"devmem remind: {exc}", file=sys.stderr)
        return ()
    try:
        if not isinstance(data, dict) or not isinstance(data["rules"], list): raise ValueError("invalid reminders config")
        rules = []
        for rule in data["rules"]:
            if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str) or not isinstance(rule.get("message"), str):
                print("devmem remind: invalid rule", file=sys.stderr); continue
            rules.append((rule["pattern"], rule["message"]))
    except Exception as exc:
        print(f"devmem remind: {exc}", file=sys.stderr)
        return ()
    return rules


def _remind_hook() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("tool_name") != "Bash": return
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str): return
        for pattern, message in _reminder_rules():
            try:
                if re.search(pattern, command):
                    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "devmem reminder: " + message}}, separators=(",", ":")))
                    return
            except re.error as exc:
                print(f"devmem remind: invalid regex: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"devmem remind: {exc}", file=sys.stderr)

def main() -> None:
    parser = argparse.ArgumentParser(prog="devmem")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp")
    sub.add_parser("ingest")
    sub.add_parser("embed")
    sub.add_parser("audit")
    panel = sub.add_parser("panel"); panel.add_argument("--port", type=int, default=8765)
    search = sub.add_parser("search"); search.add_argument("q"); search.add_argument("--limit", type=int, default=8)
    context = sub.add_parser("context")
    context.add_argument("--cwd")
    context.add_argument("--hook", action="store_true")
    remind = sub.add_parser("remind")
    remind.add_argument("--hook", action="store_true")
    args = parser.parse_args()
    if args.command == "mcp":
        from .mcp_server import run; run()
    elif args.command == "ingest":
        from .ingest.runner import ingest_all; print(f"Ingested {ingest_all()} new usage events.")
    elif args.command == "embed":
        total = 0
        with connection() as conn:
            while True:
                result = ensure_embeddings(conn)
                total += result["embedded"]
                if not result["embedded"]:
                    print(f"Embedded {total} memories; {result['pending']} pending.")
                    if result["pending"]:
                        status = getattr(get_embedder(), "status", lambda: {})()
                        print(f"devmem embed: {status.get('last_error') or 'embedder unavailable'}", file=sys.stderr)
                        raise SystemExit(1)
                    break
    elif args.command == "audit":
        with connection() as conn:
            rows = audit_memories(conn)
        if not rows:
            print("No audit findings.")
        for row in rows:
            print(f"#{row['id']}\t{row['title']}\t{row['size']} chars\t{row['updated_at']}\t{row['reason']}")
    elif args.command == "panel":
        import uvicorn
        from .panel.app import _shutdown_event, app

        class PanelServer(uvicorn.Server):
            def handle_exit(self, sig, frame):
                _shutdown_event.set()
                super().handle_exit(sig, frame)

        _shutdown_event.clear()
        config = uvicorn.Config(app, host="127.0.0.1", port=args.port, timeout_graceful_shutdown=2)
        PanelServer(config).run()
    elif args.command == "context":
        try:
            if not db_path().is_file():
                return
            briefing = _context_for_cwd(_hook_cwd() if args.hook else args.cwd or os.getcwd())
            if args.hook and briefing == _NO_CONTEXT:
                return
            if args.hook:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                          "additionalContext": f"{_HOOK_HEADER}\n{briefing}"}}))
            else:
                print(briefing)
        except Exception as exc:
            print(f"devmem context: {exc}", file=sys.stderr)
    elif args.command == "remind":
        if args.hook: _remind_hook()
    else:
        if not fts_query(args.q): return
        with connection() as conn:
            rows = hybrid_search(conn, args.q, where_sql="1=1", limit=args.limit)
        for r in rows: print(f"{r['id']} [{r['kind']}] {r['title']} — {r['snippet']}")

if __name__ == "__main__": main()
