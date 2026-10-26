from __future__ import annotations

import argparse

from .db import connection, fts_query

def main() -> None:
    parser = argparse.ArgumentParser(prog="devmem")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp")
    sub.add_parser("ingest")
    panel = sub.add_parser("panel"); panel.add_argument("--port", type=int, default=8765)
    search = sub.add_parser("search"); search.add_argument("q"); search.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    if args.command == "mcp":
        from .mcp_server import run; run()
    elif args.command == "ingest":
        from .ingest.runner import ingest_all; print(f"Ingested {ingest_all()} new usage events.")
    elif args.command == "panel":
        import uvicorn
        uvicorn.run("devmem.panel.app:app", host="127.0.0.1", port=args.port)
    else:
        if not fts_query(args.q): return
        with connection() as conn:
            rows=conn.execute("SELECT m.id,m.title,m.kind,m.project,m.updated_at,snippet(memories_fts,1,'[',']','…',16) snippet FROM memories_fts JOIN memories m ON m.id=memories_fts.rowid WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?",(fts_query(args.q),args.limit)).fetchall()
        for r in rows: print(f"{r['id']} [{r['kind']}] {r['title']} — {r['snippet']}")

if __name__ == "__main__": main()
