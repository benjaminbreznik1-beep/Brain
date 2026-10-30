"""stdio MCP server.  Memory is intentionally plain SQLite, never an external service."""
from __future__ import annotations

import asyncio
import atexit
import errno
import json
import logging
import os
import socket
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from mcp.server.fastmcp import Context, FastMCP

from . import messages

log = logging.getLogger("devmem.mcp")
from .db import connection, context_text, embed_memory, hybrid_search, is_stale, not_superseded_sql, now, project_for_cwd, restore_memory, save_memory, similar_memories, task_board as read_task_board, task_status_set as set_task_status, update_memory, validate_content, validate_review_after

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


def _call(tool: str, args: dict[str, Any], fn, ctx: Context | None = None, started: float | None = None):
    started = started if started is not None else time.monotonic()
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
        # fix(2026-10-28): bookkeeping only; a locked DB here must neither turn a committed post into an error (the agent re-posts) nor replace the real error.
        try:
            with connection() as conn:
                conn.execute("UPDATE agent_clients SET last_seen_at=? WHERE id=?", (now(), client_id))
                conn.execute("INSERT INTO tool_calls(client_id,tool,args_summary,result_count,duration_ms,ok,error,at) VALUES (?,?,?,?,?,?,?,?)",
                             (client_id, tool, json.dumps(args, ensure_ascii=False)[:1000], count, int((time.monotonic()-started)*1000), ok, error, now()))
        except Exception:
            log.warning("could not log the %s tool call", tool, exc_info=True)


def _scope(scope: str, project: str | None) -> tuple[str, tuple]:
    if scope == "global": return "project IS NULL", ()
    if scope == "project": return "project=?", (project,)
    if scope == "all": return "1=1", ()
    raise ValueError("scope must be project, global, or all")


@mcp.tool(description="Save a durable coding memory and return its id plus optional same-scope duplicate hints; merge a same-fact hint into the old memory. Never save secrets. Content is capped: put long material in a file and save a short summary plus its path. Set review_after (YYYY-MM-DD, usually about 14 days out) for status, handoff, or in-progress notes.")
def memory_save(content: str, title: str | None = None, kind: str = "note", tags: str | None = None, scope: str = "project", pinned: bool = False, review_after: str | None = None, ctx: Context | None = None) -> dict:
    def work():
        _, project = _client(); selected = None if scope == "global" else project
        if scope not in ("project", "global"): raise ValueError("scope must be project or global")
        if kind not in {"note","decision","preference","snippet","todo"}: raise ValueError("invalid kind")
        validate_content(content)
        review = validate_review_after(review_after)
        with connection() as conn:
            memory_id = save_memory(conn, content=content, title=title, kind=kind, tags=tags or "", project=selected, pinned=pinned, review_after=review, source_agent=_client_name or "mcp-client")
            result = {"id": memory_id}
            conn.commit()  # loading the embedding model can take seconds; do not hold the write lock meanwhile
            if os.environ.get("DEVMEM_EMBED") != "0":
                try:
                    if embed_memory(conn, memory_id):
                        similar = similar_memories(conn, memory_id, selected)
                        if similar: result["similar"] = similar
                except Exception as exc:  # hints are optional; the memory is already saved
                    print(f"devmem: similar check skipped: {type(exc).__name__}: {exc}"[:250], file=sys.stderr)
            return result
    return _call("memory_save", {"title": title, "kind": kind, "scope": scope, "pinned": pinned, "review_after": review_after}, work, ctx)


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
        superseded = conn.execute("SELECT from_id FROM memory_links WHERE to_id=? AND type='supersedes' ORDER BY id DESC LIMIT 1", (id,)).fetchone()
        result["superseded_by"] = superseded["from_id"] if superseded else None
        result["revisions"] = conn.execute("SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?", (id,)).fetchone()[0]
        result["links"] = [dict(link) for link in conn.execute("""SELECT l.id,l.type,'out' direction,l.to_id other_id,m.title other_title,l.note
            FROM memory_links l JOIN memories m ON m.id=l.to_id WHERE l.from_id=?
            UNION ALL SELECT l.id,l.type,'in',l.from_id,m.title,l.note
            FROM memory_links l JOIN memories m ON m.id=l.from_id WHERE l.to_id=?
            ORDER BY type,other_id""", (id, id)).fetchall()]
        return result


_LINK_TYPES = ("blocks", "depends_on", "relates_to", "overlaps", "fixes", "supersedes")


@mcp.tool(description="Link two memories with a typed relation (blocks, depends_on, relates_to, overlaps, fixes, supersedes) so related facts are found together; for supersedes, from_id is the newer memory and supersedes to_id. Use instead of repeating the other memory's content.")
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


@mcp.tool(description="Correct an existing memory when a fact changes; its prior version is kept in history. Never use for secrets. Content is capped: put long material in a file and save a short summary plus its path. Set review_after (YYYY-MM-DD, usually about 14 days out) for status, handoff, or in-progress notes.")
def memory_update(id: int, content: str | None = None, title: str | None = None, tags: str | None = None, kind: str | None = None, pinned: bool | None = None, review_after: str | None = None, ctx: Context | None = None) -> dict:
    def work():
        with connection() as conn:
            update_memory(conn, id, content=content, title=title, tags=tags, kind=kind, pinned=pinned, review_after=review_after)
        return _get(id) or {"id": id, "missing": True}
    return _call("memory_update", {"id":id, "pinned":pinned, "review_after":review_after}, work, ctx)


@mcp.tool(description="Use this instead of two memory_update calls whenever a tracked task's phase changes. It updates the board and optional task entry atomically.")
def task_status_set(board_id: int, task: str, phase: str, status: str | None = None, entry_id: int | None = None,
                    entry_title_prefix: str = "Phase 2 task", dry_run: bool = False, ctx: Context | None = None) -> dict:
    def work():
        with connection() as conn:
            return set_task_status(conn, board_id, task, phase, status=status, entry_id=entry_id,
                                   entry_title_prefix=entry_title_prefix, dry_run=dry_run)
    return _call("task_status_set", {"board_id": board_id, "task": task, "entry_id": entry_id, "dry_run": dry_run}, work, ctx)


@mcp.tool(description="Return a task board's parsed sections, numbered entries, phases, and status date.")
def task_board(board_id: int, ctx: Context | None = None) -> dict:
    return _call("task_board", {"board_id": board_id}, lambda: _task_board(board_id), ctx)


def _task_board(board_id: int) -> dict:
    with connection() as conn:
        return read_task_board(conn, board_id)


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
            restore_memory(conn, id, revision_id, _client_name or "mcp-client")
        return _get(id)
    return _call("memory_restore", {"id": id, "revision_id": revision_id}, work, ctx)


@mcp.tool(description="List recent memories for orientation; if project scope is empty, retry scope='all'. Search is better for a specific question.")
def memory_list(limit: int = 20, kind: str | None = None, scope: str = "project", ctx: Context | None = None) -> list[dict]:
    def work():
        _, project = _client(); where, params = _scope(scope, project)
        if kind: where += " AND kind=?"; params += (kind,)
        with connection() as conn:
            return [{**dict(r), "stale": is_stale(r["review_after"])} for r in conn.execute(f"""SELECT id,title,kind,project,tags,pinned,review_after,updated_at FROM memories
                WHERE {where} AND {not_superseded_sql('memories')} ORDER BY updated_at DESC LIMIT ?""", (*params, max(1,min(limit,100)))).fetchall()]
    return _call("memory_list", {"limit":limit,"kind":kind,"scope":scope}, work, ctx)


@mcp.tool(description="Get a compact briefing before non-trivial work: all pinned memories, recent project context, and global preferences.")
def project_context(ctx: Context | None = None) -> str:
    def work():
        _, project = _client()
        with connection() as conn:
            return context_text(conn, project)
    return _call("project_context", {}, work, ctx)


@mcp.tool(description="Return the latest automatic-maintenance run and unresolved flags for the current project. This tool never starts maintenance.")
def maintenance_status(ctx: Context | None = None) -> dict:
    def work():
        from .maintenance import OPEN_FLAG_SQL, attention
        _, project = _client()
        with connection() as conn:
            last = conn.execute("SELECT * FROM maintenance_runs WHERE status NOT IN ('requested','done','skipped') ORDER BY id DESC LIMIT 1").fetchone()
            flags = conn.execute(f"""SELECT c.id,c.memory_id,c.target_id,c.summary,c.blocked_by,c.new_title,c.new_content,c.created_at,m.title memory_title
                FROM maintenance_changes c JOIN memories m ON m.id=c.memory_id
                WHERE {OPEN_FLAG_SQL} AND (m.project=? OR m.project IS NULL) ORDER BY c.id DESC LIMIT 50""", (project,)).fetchall()
            return {"last_run": dict(last) if last else None, "pending_flags": [dict(row) for row in flags], "failing": attention(conn)["failing_since"]}
    return _call("maintenance_status", {}, work, ctx)


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


# ---------------------------------------------------------------- messages between agents

WAIT_POLL_S = 2.0
WAIT_MAX_S = 60


def _me(sender: str | None) -> str:
    """Subagents share the parent's MCP connection, so only a self-declared name tells them apart."""
    return (sender or "").strip() or _client_name or "mcp-client"


def _sender_kind(declared: bool) -> str:
    if "codex" in (_client_name or "").lower(): return "codex"
    return "subagent" if declared else "claude"


@mcp.tool(description="Post a message, finding, question or hand-off for other agents and the user (they see it in the devmem panel). subject: one short line saying what happened, for example 'tests pass, 2 fixes left' — this is what the user reads; it is derived from the body if omitted. to: an agent name, '*' for everyone, '#channel', or 'user'. reply_to joins that message's thread and marks a question answered. Returns id and thread_id: to wait for an answer, call message_wait(thread_id, after_id=id). sender: your agent type/name; pass it when you are a subagent, since subagents share one connection. Never include secrets; bodies are capped at 8,000 characters.")
def message_post(body: str, to: str = "*", subject: str | None = None, kind: str = "message", reply_to: int | None = None, sender: str | None = None, ctx: Context | None = None) -> dict:
    def work():
        _, project = _client()
        declared = bool((sender or "").strip()) and sender.strip() != _client_name
        with connection() as conn:
            message_id = messages.post(conn, body, sender=_me(sender), sender_kind=_sender_kind(declared), to=to, subject=subject, kind=kind,
                                       reply_to=reply_to, project=project, declared=declared)
            return {"id": message_id, "thread_id": conn.execute("SELECT thread_id FROM messages WHERE id=?", (message_id,)).fetchone()[0]}
    return _call("message_post", {"to": to, "kind": kind, "reply_to": reply_to, "sender": sender}, work, ctx)


@mcp.tool(description="Check messages for you: those addressed to you, to '*', or to channels you posted in. Call at the start of work and before finishing. With unread_only (the default) the returned messages are marked read for you; unread_only=False re-reads history without marking anything. reader: the same name you pass as sender (subagents share one connection).")
def message_inbox(unread_only: bool = True, limit: int = 20, reader: str | None = None, ctx: Context | None = None) -> list[dict]:
    def work():
        with connection() as conn:
            return messages.inbox(conn, _me(reader), unread_only, limit)
    return _call("message_inbox", {"unread_only": unread_only, "limit": limit, "reader": reader}, work, ctx)


@mcp.tool(description="Return every message of one thread, oldest first.")
def message_thread(thread_id: int, ctx: Context | None = None) -> list[dict]:
    def work():
        with connection() as conn:
            return messages.thread(conn, thread_id)
    return _call("message_thread", {"thread_id": thread_id}, work, ctx)


@mcp.tool(description="Wait for a reply in a thread instead of sleeping: returns messages newer than after_id as soon as one lands (checked every 2 s), or timed_out after timeout_s (max 60). Pass after_id = the id message_post returned, then the last_id of each result on the next call, so no reply is missed. Without after_id it starts after your own newest message in the thread (reader = your sender name), else after the thread's first message.")
async def message_wait(thread_id: int, timeout_s: int = 60, after_id: int | None = None, reader: str | None = None, ctx: Context | None = None) -> dict:
    started = time.monotonic()
    timeout = max(0, min(int(timeout_s), WAIT_MAX_S))
    args = {"thread_id": thread_id, "timeout_s": timeout_s, "after_id": after_id, "reader": reader}

    def read(after: int | None):
        with connection() as conn:
            if after is None:
                if not conn.execute("SELECT 1 FROM messages WHERE thread_id=?", (thread_id,)).fetchone(): raise ValueError(f"thread {thread_id} does not exist")
                # fix(2026-10-28): the baseline used to be the newest id at call time, so a reply that landed before the wait was never returned.
                return messages.wait_baseline(conn, thread_id, _me(reader))
            rows = messages.newer(conn, thread_id, after)
            if rows: messages.mark_read(conn, [row["id"] for row in rows], _me(reader))
            return rows

    try:
        _client(ctx)
        after = await asyncio.to_thread(read, None) if after_id is None else int(after_id)
        if after_id is not None: await asyncio.to_thread(read, None)  # still reject an unknown thread
        while True:
            # Async with a thread hop: a blocking wait would stall every other tool call on this shared connection.
            replies = await asyncio.to_thread(read, after)
            if replies or time.monotonic() - started >= timeout:
                result = {"thread_id": thread_id, "messages": replies, "timed_out": not replies, "last_id": max([after, *(m["id"] for m in replies)])}
                break
            await asyncio.sleep(min(WAIT_POLL_S, max(0.0, timeout - (time.monotonic() - started))))
    except Exception as exc:
        error = exc
        def fail(): raise error
        return _call("message_wait", args, fail, ctx, started)
    return _call("message_wait", args, lambda: result, ctx, started)


@mcp.tool(description="Close a thread once it is resolved; the user can reopen it from the panel.")
def message_close(thread_id: int, ctx: Context | None = None) -> dict:
    def work():
        with connection() as conn:
            return {"closed": messages.close(conn, thread_id)}
    return _call("message_close", {"thread_id": thread_id}, work, ctx)


def _shutdown() -> None:
    if _client_id is not None:
        with connection() as conn: conn.execute("UPDATE agent_clients SET ended_at=?,last_seen_at=? WHERE id=?", (now(),now(),_client_id))

atexit.register(_shutdown)

def run() -> None:
    mcp.run(transport="stdio")
