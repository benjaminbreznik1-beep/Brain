"""stdio MCP server.  Memory is intentionally plain SQLite, never an external service."""
from __future__ import annotations

import atexit
import json
import os
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .db import connection, hybrid_search, now, project_for_cwd

mcp = FastMCP("devmem", instructions="Local long-term coding memory. Search before non-trivial work; save durable decisions, preferences, and hard-won gotchas. Never store secrets, credentials, or private keys.")
_client_id: int | None = None
_client_name: str | None = None


def _client(ctx: Context | None = None) -> tuple[int, str | None]:
    global _client_id, _client_name
    cwd = os.getcwd()
    project = project_for_cwd(cwd)
    info = getattr(getattr(getattr(ctx, "session", None), "client_params", None), "clientInfo", None) if ctx else None
    if _client_id is None:
        with connection() as conn:
            cur = conn.execute("INSERT INTO agent_clients(client_name,client_version,pid,cwd,project,started_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",
                               (info.name if info else "mcp-client", info.version if info else None, os.getpid(), cwd, project, now(), now()))
            _client_id = cur.lastrowid
        _client_name = info.name if info else None
    elif info and _client_name is None:
        # The first call may arrive without context; record the initialize clientInfo once it is available.
        with connection() as conn:
            conn.execute("UPDATE agent_clients SET client_name=?,client_version=? WHERE id=?", (info.name, info.version, _client_id))
        _client_name = info.name
    return _client_id, project


def _call(tool: str, args: dict[str, Any], fn, ctx: Context | None = None):
    started = time.monotonic()
    client_id, _ = _client(ctx)
    try:
        result = fn()
        count = len(result) if isinstance(result, list) else 1
        ok, error = 1, None
        return result
    except Exception as exc:
        count, ok, error = 0, 0, str(exc)
        raise
    finally:
        with connection() as conn:
            conn.execute("UPDATE agent_clients SET last_seen_at=? WHERE id=?", (now(), client_id))
            conn.execute("INSERT INTO tool_calls(client_id,tool,args_summary,result_count,duration_ms,ok,error,at) VALUES (?,?,?,?,?,?,?,?)",
                         (client_id, tool, json.dumps(args, ensure_ascii=False)[:1000], count, int((time.monotonic()-started)*1000), ok, error, now()))


def _scope(scope: str, project: str | None) -> tuple[str, tuple]:
    if scope == "global": return "project IS NULL", ()
    if scope == "project": return "project=?", (project,)
    if scope == "all": return "1=1", ()
    raise ValueError("scope must be project, global, or all")


@mcp.tool(description="Save a durable coding memory. Use after discovering a decision, user preference, gotcha, or reusable snippet; never save secrets.")
def memory_save(content: str, title: str | None = None, kind: str = "note", tags: str | None = None, scope: str = "project", ctx: Context | None = None) -> dict:
    def work():
        _, project = _client(); selected = None if scope == "global" else project
        if scope not in ("project", "global"): raise ValueError("scope must be project or global")
        if kind not in {"note","decision","preference","snippet","todo"}: raise ValueError("invalid kind")
        with connection() as conn:
            cur = conn.execute("INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at,source_agent) VALUES (?,?,?,?,?,?,?,?)",
              (selected, kind, title or content[:80], content, tags or "", now(), now(), _client_name or "mcp-client"))
            return {"id": cur.lastrowid}
    return _call("memory_save", {"title": title, "kind": kind, "scope": scope}, work, ctx)


@mcp.tool(description="Search memory before starting non-trivial work, especially for prior decisions, conventions, and gotchas.")
def memory_search(query: str, limit: int = 8, kind: str | None = None, scope: str = "project", ctx: Context | None = None) -> list[dict]:
    def work():
        _, project = _client(); where, params = _scope(scope, project)
        with connection() as conn:
            return hybrid_search(conn, query, where_sql=where, params=params, kind=kind,
                                 limit=max(1, min(limit, 50)))
    return _call("memory_search", {"query": query, "limit": limit, "kind": kind, "scope": scope}, work, ctx)


@mcp.tool(description="Fetch a memory by id when a search result needs its full content.")
def memory_get(id: int, ctx: Context | None = None) -> dict | None:
    return _call("memory_get", {"id": id}, lambda: _get(id), ctx)

def _get(id: int):
    with connection() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None


@mcp.tool(description="Correct or refine an existing memory after information changes; do not use for secrets.")
def memory_update(id: int, content: str | None = None, title: str | None = None, tags: str | None = None, kind: str | None = None, ctx: Context | None = None) -> dict:
    def work():
        fields = {k:v for k,v in {"content":content,"title":title,"tags":tags,"kind":kind}.items() if v is not None}
        if kind and kind not in {"note","decision","preference","snippet","todo"}: raise ValueError("invalid kind")
        if not fields: return _get(id) or {"id": id, "missing": True}
        fields["updated_at"] = now()
        with connection() as conn:
            conn.execute("UPDATE memories SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), id))
        return _get(id) or {"id": id, "missing": True}
    return _call("memory_update", {"id":id}, work, ctx)


@mcp.tool(description="Delete a memory only when it is obsolete, duplicate, or incorrect.")
def memory_delete(id: int, ctx: Context | None = None) -> dict:
    def work():
        with connection() as conn: cur = conn.execute("DELETE FROM memories WHERE id=?", (id,))
        return {"deleted": bool(cur.rowcount)}
    return _call("memory_delete", {"id":id}, work, ctx)


@mcp.tool(description="List recent memories for orientation. Search is better when you have a specific question.")
def memory_list(limit: int = 20, kind: str | None = None, scope: str = "project", ctx: Context | None = None) -> list[dict]:
    def work():
        _, project = _client(); where, params = _scope(scope, project)
        if kind: where += " AND kind=?"; params += (kind,)
        with connection() as conn:
            return [dict(r) for r in conn.execute(f"SELECT id,title,kind,project,tags,updated_at FROM memories WHERE {where} ORDER BY updated_at DESC LIMIT ?", (*params, max(1,min(limit,100)))).fetchall()]
    return _call("memory_list", {"limit":limit,"kind":kind,"scope":scope}, work, ctx)


@mcp.tool(description="Get a compact current-project briefing before non-trivial work: recent decisions, preferences, and open todos.")
def project_context(ctx: Context | None = None) -> str:
    def work():
        _, project = _client()
        with connection() as conn:
            rows = conn.execute("""SELECT kind,title,content FROM memories WHERE project=? AND kind IN ('decision','preference','todo','note')
                ORDER BY kind='note', updated_at DESC LIMIT 20""", (project,)).fetchall()
            global_rows = conn.execute("SELECT kind,title,content FROM memories WHERE project IS NULL AND kind IN ('decision','preference') ORDER BY updated_at DESC LIMIT 40").fetchall()
        text = "\n".join(f"[{r['kind']}] {r['title']}: {r['content'][:200]}" for r in rows)[:1500]
        global_text = "\n".join(f"[{r['kind']}] {r['title']}" for r in global_rows)[:3000]  # titles state the rule; memory_get has the detail
        parts = [f"## Project\n{text}" if text else "", f"## Global preferences\n{global_text}" if global_text else ""]
        return "\n\n".join(p for p in parts if p) or "No saved project context."
    return _call("project_context", {}, work, ctx)


def _shutdown() -> None:
    if _client_id is not None:
        with connection() as conn: conn.execute("UPDATE agent_clients SET ended_at=?,last_seen_at=? WHERE id=?", (now(),now(),_client_id))

atexit.register(_shutdown)

def run() -> None:
    mcp.run(transport="stdio")
