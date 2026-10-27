from __future__ import annotations

import argparse
import json
import os
import sys

from .db import context_text, connection, db_path, ensure_embeddings, fts_query, hybrid_search, project_for_cwd, readonly_connection
from .embed import get_embedder


_NO_CONTEXT = "No saved project context."
_HOOK_HEADER = "devmem memory (auto-loaded at session start; use memory_search / memory_get for more):"


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

def main() -> None:
    parser = argparse.ArgumentParser(prog="devmem")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp")
    sub.add_parser("ingest")
    sub.add_parser("embed")
    panel = sub.add_parser("panel"); panel.add_argument("--port", type=int, default=8765)
    search = sub.add_parser("search"); search.add_argument("q"); search.add_argument("--limit", type=int, default=8)
    context = sub.add_parser("context")
    context.add_argument("--cwd")
    context.add_argument("--hook", action="store_true")
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
    else:
        if not fts_query(args.q): return
        with connection() as conn:
            rows = hybrid_search(conn, args.q, where_sql="1=1", limit=args.limit)
        for r in rows: print(f"{r['id']} [{r['kind']}] {r['title']} — {r['snippet']}")

if __name__ == "__main__": main()
