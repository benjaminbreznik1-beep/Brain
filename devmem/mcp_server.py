"""stdio MCP server.  Memory is intentionally plain SQLite, never an external service."""
from __future__ import annotations

import atexit
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from mcp.server.fastmcp import Context, FastMCP

from .db import connection, context_text, hybrid_search, now, project_for_cwd

mcp = FastMCP("devmem", instructions="Local long-term coding memory. Search before non-trivial work; save durable decisions, preferences, and hard-won gotchas. Never store secrets, credentials, or private keys.")
_client_id: int | None = None
_client_name: str | None = None


class _PanelNoRedirect(HTTPRedirectHandler):
    """Keep a panel redirect from causing an unreviewed follow-up request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# This opener is deliberately used only by the two fixed loopback URLs below.
_PANEL_OPENER = build_opener(ProxyHandler({}), _PanelNoRedirect())


def _panel_urls() -> tuple[str, str]:
    """Return the only panel URLs this process is permitted to request."""
    raw_port = os.environ.get("DEVMEM_PANEL_PORT", "8765")
    try:
        port = int(raw_port)
    except ValueError:
        port = 8765
    if not 1 <= port <= 65535:
        port = 8765
    base = f"http://127.0.0.1:{port}"
    return base + "/api/ado", base + "/ado/refresh"


def _panel_snapshot(refresh: bool) -> dict[str, Any]:
    """Read the panel-owned ADO cache without following any redirect."""
    api_url, refresh_url = _panel_urls()
    if refresh:
        try:
            with _PANEL_OPENER.open(Request(refresh_url, method="POST"), timeout=20) as response:
                # A direct 2xx response is also harmless if the panel implementation changes.
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status not in (200, 201, 202, 204, 303):
                    raise OSError("panel refresh failed")
        except HTTPError as exc:
            # The panel normally returns this redirect.  It is intentionally not followed.
            if exc.code != 303:
                raise
    with _PANEL_OPENER.open(Request(api_url, method="GET"), timeout=3) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("panel returned an invalid snapshot")
    return data


def _ado_unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _ado_pr(pr: dict[str, Any], pending_reviewers: bool = False) -> dict[str, Any]:
    item = {key: pr.get(key) for key in ("id", "title", "repo", "url", "age_label")}
    if pending_reviewers:
        item["pending_reviewers"] = list(pr.get("pending_reviewers") or [])
    return item


def _ado_status(snapshot: dict[str, Any], section: str) -> dict[str, Any]:
    if not snapshot.get("configured"):
        return _ado_unavailable("ADO not configured (no token)")
    result: dict[str, Any] = {
        "available": True,
        "refreshed_at": snapshot.get("refreshed_at"),
        "stale": bool(snapshot.get("stale")),
        "error": snapshot.get("error"),
        "project_active_count": snapshot.get("project_active_count", 0),
    }
    if section in ("all", "waiting_for_me"):
        result["waiting_for_me"] = {
            "to_review": [_ado_pr(pr) for pr in snapshot.get("to_review", [])],
            "approved_not_completed": [_ado_pr(pr) for pr in snapshot.get("my_approved_not_completed", [])],
            "changes_requested": [_ado_pr(pr) for pr in snapshot.get("my_changes_requested", [])],
        }
    if section in ("all", "waiting_on_others"):
        result["waiting_on_others"] = [
            _ado_pr(pr, pending_reviewers=True) for pr in snapshot.get("my_waiting_on_others", [])
        ]
    if section in ("all", "tasks"):
        tasks = [
            {key: task.get(key) for key in ("id", "title", "type", "state", "url")}
            for task in snapshot.get("open_tasks", [])
        ]
        tasks.sort(key=lambda task: (task["state"] != "In Progress", task["state"] or "", task["id"]))
        result["tasks"] = tasks
        result["tasks_by_state"] = {
            state: len(items) for state, items in (snapshot.get("open_tasks_by_state") or {}).items()
        }
    return result


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
            return context_text(conn, project)
    return _call("project_context", {}, work, ctx)


@mcp.tool(description="Read-only view of the user's Azure DevOps: PRs waiting for them, their PRs waiting on others, and their open work items. Use when asked about tasks, PRs, or what to work on.")
def ado_status(section: str = "all", refresh: bool = False, ctx: Context | None = None) -> dict:
    """Return a compact, token-free projection of the running panel's ADO cache."""
    def work():
        if section not in {"all", "waiting_for_me", "waiting_on_others", "tasks"}:
            raise ValueError("section must be all, waiting_for_me, waiting_on_others, or tasks")
        try:
            snapshot = _panel_snapshot(refresh)
        except (HTTPError, URLError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            # Do not expose upstream messages; they can include arbitrary response data.
            port = _panel_urls()[0].rsplit(":", 1)[1].split("/", 1)[0]
            return _ado_unavailable(f"devmem panel not running on 127.0.0.1:{port}")
        return _ado_status(snapshot, section)
    return _call("ado_status", {"section": section, "refresh": refresh}, work, ctx)


def _shutdown() -> None:
    if _client_id is not None:
        with connection() as conn: conn.execute("UPDATE agent_clients SET ended_at=?,last_seen_at=? WHERE id=?", (now(),now(),_client_id))

atexit.register(_shutdown)

def run() -> None:
    mcp.run(transport="stdio")
