"""stdio MCP server.  Memory is intentionally plain SQLite, never an external service."""
from __future__ import annotations

import atexit
import errno
import json
import os
import socket
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from mcp.server.fastmcp import Context, FastMCP

from .db import connection, context_text, embed_memory, hybrid_search, now, project_for_cwd, similar_memories

mcp = FastMCP("devmem", instructions="Local long-term coding memory. Search before non-trivial work; if project search or list is empty, retry scope='all' because the server cwd may not be the repo. After memory_save, inspect similar; if it is the same fact, update the old memory and delete the new one. Prefer memory_update when facts change; history is available through memory_history and memory_restore. Link related memories instead of repeating their content. Pinned memories are already in the briefing, so pin only what every session needs. Never store secrets, credentials, or private keys.")
_client_id: int | None = None
_client_name: str | None = None


class _PanelNoRedirect(HTTPRedirectHandler):
    """Keep a panel redirect from causing an unreviewed follow-up request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# This opener is deliberately used only by the two fixed loopback URLs below.
_PANEL_OPENER = build_opener(ProxyHandler({}), _PanelNoRedirect())


def _is_timeout(exc: BaseException) -> bool:
    reason = exc.reason if isinstance(exc, URLError) else exc
    return isinstance(reason, (TimeoutError, socket.timeout))


def _is_connection_refused(exc: URLError) -> bool:
    reason = exc.reason
    return isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) == errno.ECONNREFUSED


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
    refresh_timed_out = False
    if refresh:
        try:
            with _PANEL_OPENER.open(Request(refresh_url, method="POST"), timeout=20) as response:
                # A direct 2xx response is also harmless if the panel implementation changes.
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status not in (200, 201, 202, 204):
                    raise OSError("panel refresh failed")
        except HTTPError as exc:
            # The panel normally returns this redirect.  It is intentionally not followed.
            if exc.code != 303:
                raise
        except (TimeoutError, socket.timeout):
            refresh_timed_out = True
        except URLError as exc:
            if not _is_timeout(exc):
                raise
            refresh_timed_out = True
    with _PANEL_OPENER.open(Request(api_url, method="GET"), timeout=3) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("panel returned an invalid snapshot")
    if refresh_timed_out:
        # This private marker is removed before the MCP response is produced.
        data["_refresh_timed_out"] = True
    return data


def _ado_unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _ado_pr(pr: dict[str, Any], pending_reviewers: bool = False) -> dict[str, Any]:
    item = {key: pr.get(key) for key in ("id", "title", "repo", "url", "age_label")}
    if pending_reviewers:
        item["pending_reviewers"] = list(pr.get("pending_reviewers") or [])
    return item


def _ado_status(snapshot: dict[str, Any], section: str) -> dict[str, Any]:
    if snapshot.get("pending"):
        return _ado_unavailable("ADO data not loaded yet (first refresh pending)")
    if not snapshot.get("configured"):
        missing = snapshot.get("missing")
        reasons = []
        if snapshot.get("config_error"):
            reasons.append("ADO config error: " + str(snapshot["config_error"]))
        if snapshot.get("token_error"):
            reasons.append("ADO token error: " + str(snapshot["token_error"]))
        if isinstance(missing, list):
            labels = []
            if "token" in missing: labels.append("token")
            if "config" in missing: labels.append("organization/project")
            if labels:
                reasons.append("ADO not configured (missing " + " and ".join(labels) + ")")
        if snapshot.get("error"):
            reasons.append("ADO error: " + str(snapshot["error"]))
        if reasons:
            return _ado_unavailable("; ".join(reasons))
        return _ado_unavailable("ADO not configured (missing token or organization/project; see README 'Azure DevOps')")
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


@mcp.tool(description="Save a durable coding memory and return its id plus optional same-scope duplicate hints; merge a same-fact hint into the old memory. Never save secrets.")
def memory_save(content: str, title: str | None = None, kind: str = "note", tags: str | None = None, scope: str = "project", pinned: bool = False, ctx: Context | None = None) -> dict:
    def work():
        _, project = _client(); selected = None if scope == "global" else project
        if scope not in ("project", "global"): raise ValueError("scope must be project or global")
        if kind not in {"note","decision","preference","snippet","todo"}: raise ValueError("invalid kind")
        with connection() as conn:
            cur = conn.execute("INSERT INTO memories(project,kind,title,content,tags,pinned,created_at,updated_at,source_agent) VALUES (?,?,?,?,?,?,?,?,?)",
              (selected, kind, title or content[:80], content, tags or "", int(pinned), now(), now(), _client_name or "mcp-client"))
            result = {"id": cur.lastrowid}
            conn.commit()  # loading the embedding model can take seconds; do not hold the write lock meanwhile
            if os.environ.get("DEVMEM_EMBED") != "0":
                try:
                    if embed_memory(conn, cur.lastrowid):
                        similar = similar_memories(conn, cur.lastrowid, selected)
                        if similar: result["similar"] = similar
                except Exception as exc:  # hints are optional; the memory is already saved
                    print(f"devmem: similar check skipped: {type(exc).__name__}: {exc}"[:250], file=sys.stderr)
            return result
    return _call("memory_save", {"title": title, "kind": kind, "scope": scope, "pinned": pinned}, work, ctx)


@mcp.tool(description="Search before non-trivial work. If project scope is empty, retry scope='all' because the server cwd may differ from the repo. Uses hybrid ranking; match is keyword, semantic, or both.")
def memory_search(query: str, limit: int = 8, kind: str | None = None, scope: str = "project", ctx: Context | None = None) -> list[dict]:
    def work():
        _, project = _client(); where, params = _scope(scope, project)
        with connection() as conn:
            return hybrid_search(conn, query, where_sql=where, params=params, kind=kind,
                                 limit=max(1, min(limit, 50)))
    return _call("memory_search", {"query": query, "limit": limit, "kind": kind, "scope": scope}, work, ctx)


@mcp.tool(description="Fetch a memory by id when a search result needs its full content and linked facts.")
def memory_get(id: int, ctx: Context | None = None) -> dict | None:
    return _call("memory_get", {"id": id}, lambda: _get(id), ctx)

def _get(id: int):
    with connection() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        if not row: return None
        result = dict(row)
        result["revisions"] = conn.execute("SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?", (id,)).fetchone()[0]
        result["links"] = [dict(link) for link in conn.execute("""SELECT l.id,l.type,'out' direction,l.to_id other_id,m.title other_title,l.note
            FROM memory_links l JOIN memories m ON m.id=l.to_id WHERE l.from_id=?
            UNION ALL SELECT l.id,l.type,'in',l.from_id,m.title,l.note
            FROM memory_links l JOIN memories m ON m.id=l.from_id WHERE l.to_id=?
            ORDER BY type,other_id""", (id, id)).fetchall()]
        return result


_LINK_TYPES = ("blocks", "depends_on", "relates_to", "overlaps", "fixes", "supersedes")


@mcp.tool(description="Link two memories with a typed relation (blocks, depends_on, relates_to, overlaps, fixes, supersedes) so related facts are found together; use instead of repeating the other memory's content.")
def memory_link(from_id: int, to_id: int, type: str, note: str = "", ctx: Context | None = None) -> dict:
    def work():
        if from_id == to_id: raise ValueError("cannot link a memory to itself")
        if type not in _LINK_TYPES: raise ValueError("type must be " + ", ".join(_LINK_TYPES))
        with connection() as conn:
            if not conn.execute("SELECT 1 FROM memories WHERE id=?", (from_id,)).fetchone() or not conn.execute("SELECT 1 FROM memories WHERE id=?", (to_id,)).fetchone():
                raise ValueError("both memories must exist")
            conn.execute("""INSERT INTO memory_links(from_id,to_id,type,note,created_at) VALUES(?,?,?,?,?)
                ON CONFLICT(from_id,to_id,type) DO UPDATE SET note=excluded.note""", (from_id, to_id, type, note, now()))
            return dict(conn.execute("SELECT * FROM memory_links WHERE from_id=? AND to_id=? AND type=?", (from_id, to_id, type)).fetchone())
    return _call("memory_link", {"from_id": from_id, "to_id": to_id, "type": type}, work, ctx)


@mcp.tool(description="Remove one typed link between memories, or every link type between them when type is omitted.")
def memory_unlink(from_id: int, to_id: int, type: str | None = None, ctx: Context | None = None) -> dict:
    def work():
        if type is not None and type not in _LINK_TYPES: raise ValueError("type must be " + ", ".join(_LINK_TYPES))
        with connection() as conn:
            cur = conn.execute("DELETE FROM memory_links WHERE from_id=? AND to_id=?" + (" AND type=?" if type is not None else ""), (from_id, to_id, type) if type is not None else (from_id, to_id))
        return {"removed": cur.rowcount}
    return _call("memory_unlink", {"from_id": from_id, "to_id": to_id, "type": type}, work, ctx)


@mcp.tool(description="Correct an existing memory when a fact changes; its prior version is kept in history. Never use for secrets.")
def memory_update(id: int, content: str | None = None, title: str | None = None, tags: str | None = None, kind: str | None = None, pinned: bool | None = None, ctx: Context | None = None) -> dict:
    def work():
        fields = {k:v for k,v in {"content":content,"title":title,"tags":tags,"kind":kind,"pinned":pinned}.items() if v is not None}
        if kind and kind not in {"note","decision","preference","snippet","todo"}: raise ValueError("invalid kind")
        if not fields: return _get(id) or {"id": id, "missing": True}
        fields["updated_at"] = now()
        with connection() as conn:
            conn.execute("UPDATE memories SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), id))
        return _get(id) or {"id": id, "missing": True}
    return _call("memory_update", {"id":id, "pinned":pinned}, work, ctx)


@mcp.tool(description="Delete a memory only when it is obsolete, duplicate, or incorrect.")
def memory_delete(id: int, ctx: Context | None = None) -> dict:
    def work():
        with connection() as conn: cur = conn.execute("DELETE FROM memories WHERE id=?", (id,))
        return {"deleted": bool(cur.rowcount)}
    return _call("memory_delete", {"id":id}, work, ctx)


@mcp.tool(description="Return prior versions of a memory, newest first, including after deletion; use before restoring or auditing a change.")
def memory_history(id: int, limit: int = 10, ctx: Context | None = None) -> list[dict]:
    def work():
        with connection() as conn:
            return [dict(row) for row in conn.execute("""SELECT id,op,revised_at,title,kind,project,tags,pinned,created_at,content
                FROM memory_revisions WHERE memory_id=? ORDER BY id DESC LIMIT ?""", (id, max(1, min(limit, 50)))).fetchall()]
    return _call("memory_history", {"id": id, "limit": limit}, work, ctx)


@mcp.tool(description="Restore one history revision and return the restored memory; use to undo an update or recover a deleted memory.")
def memory_restore(id: int, revision_id: int, ctx: Context | None = None) -> dict:
    def work():
        with connection() as conn:
            row = conn.execute("SELECT * FROM memory_revisions WHERE id=? AND memory_id=?", (revision_id, id)).fetchone()
            if not row: raise ValueError("revision does not belong to memory")
            existing = conn.execute("SELECT created_at FROM memories WHERE id=?", (id,)).fetchone()
            if existing and row["created_at"] is not None and existing["created_at"] != row["created_at"]:
                # SQLite reuses the highest id after a delete, so this id now belongs to a different memory.
                raise ValueError("memory id was reused by a newer memory; save the revision as a new memory instead")
            if existing:
                conn.execute("UPDATE memories SET project=?,kind=?,title=?,content=?,tags=?,pinned=?,updated_at=? WHERE id=?",
                             (row["project"], row["kind"], row["title"], row["content"], row["tags"], row["pinned"], now(), id))
            else:
                conn.execute("""INSERT INTO memories(id,project,kind,title,content,tags,pinned,created_at,updated_at,source_agent)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (id, row["project"], row["kind"], row["title"], row["content"], row["tags"], row["pinned"], row["created_at"] or now(), now(), _client_name or "mcp-client"))
                if row["op"] == "delete":
                    conn.execute("""INSERT OR IGNORE INTO memory_links(from_id,to_id,type,note,created_at)
                        SELECT from_id,to_id,type,note,created_at FROM memory_link_revisions
                        WHERE memory_revision_id=? AND (from_id=? OR to_id=?)
                          AND EXISTS(SELECT 1 FROM memories WHERE id=CASE WHEN from_id=? THEN to_id ELSE from_id END)""",
                        (revision_id, id, id, id))
        return _get(id)
    return _call("memory_restore", {"id": id, "revision_id": revision_id}, work, ctx)


@mcp.tool(description="List recent memories for orientation; if project scope is empty, retry scope='all'. Search is better for a specific question.")
def memory_list(limit: int = 20, kind: str | None = None, scope: str = "project", ctx: Context | None = None) -> list[dict]:
    def work():
        _, project = _client(); where, params = _scope(scope, project)
        if kind: where += " AND kind=?"; params += (kind,)
        with connection() as conn:
            return [dict(r) for r in conn.execute(f"SELECT id,title,kind,project,tags,pinned,updated_at FROM memories WHERE {where} ORDER BY updated_at DESC LIMIT ?", (*params, max(1,min(limit,100)))).fetchall()]
    return _call("memory_list", {"limit":limit,"kind":kind,"scope":scope}, work, ctx)


@mcp.tool(description="Get a compact briefing before non-trivial work: all pinned memories, recent project context, and global preferences.")
def project_context(ctx: Context | None = None) -> str:
    def work():
        _, project = _client()
        with connection() as conn:
            return context_text(conn, project)
    return _call("project_context", {}, work, ctx)


@mcp.tool(description="Read-only Azure DevOps summary served from the running devmem panel's cache (5-min refresh). section: all | waiting_for_me (PRs to review, own PRs approved-not-completed or with changes requested) | waiting_on_others | tasks (open work items assigned to you in the configured project). refresh=true forces a live fetch (slow). Returns available:false with a reason if the panel is down, ADO is unconfigured, or data is not loaded yet.")
def ado_status(section: str = "all", refresh: bool = False, ctx: Context | None = None) -> dict:
    """Return a compact, token-free projection of the running panel's ADO cache."""
    def work():
        if section not in {"all", "waiting_for_me", "waiting_on_others", "tasks"}:
            raise ValueError("section must be all, waiting_for_me, waiting_on_others, or tasks")
        try:
            snapshot = _panel_snapshot(refresh)
        except HTTPError as exc:
            if exc.code != 303:
                return _ado_unavailable(f"panel returned HTTP {exc.code}")
            return _ado_unavailable("panel refresh redirected unexpectedly")
        except URLError as exc:
            port = _panel_urls()[0].rsplit(":", 1)[1].split("/", 1)[0]
            if _is_connection_refused(exc):
                return _ado_unavailable(f"devmem panel not running on 127.0.0.1:{port}")
            if _is_timeout(exc):
                return _ado_unavailable("panel request timed out")
            return _ado_unavailable(f"panel request failed ({type(exc.reason).__name__})")
        except (TimeoutError, socket.timeout):
            return _ado_unavailable("panel request timed out")
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return _ado_unavailable("panel returned an invalid snapshot")
        refresh_timed_out = bool(snapshot.pop("_refresh_timed_out", False))
        result = _ado_status(snapshot, section)
        if refresh_timed_out:
            result["note"] = "refresh timed out; showing cached data"
        return result
    return _call("ado_status", {"section": section, "refresh": refresh}, work, ctx)


def _shutdown() -> None:
    if _client_id is not None:
        with connection() as conn: conn.execute("UPDATE agent_clients SET ended_at=?,last_seen_at=? WHERE id=?", (now(),now(),_client_id))

atexit.register(_shutdown)

def run() -> None:
    mcp.run(transport="stdio")
