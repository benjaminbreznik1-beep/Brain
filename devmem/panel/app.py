from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..db import connection, ensure_embeddings, fts_query, hybrid_search, now
from ..embed import get_embedder
from ..ingest.runner import ingest_all
from ..ado import AdoClient, AdoError, empty_snapshot
from . import state as live

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
log = logging.getLogger("devmem.panel")

INGEST_EVERY_S = 5
STREAM_EVERY_S = 2.5
STATE_TTL_S = 2.0
ADO_REFRESH_S = 5 * 60

# Set by the CLI-owned Uvicorn server before it begins graceful shutdown.  SSE
# clients use this to leave their generators promptly instead of holding it up.
_shutdown_event = threading.Event()


def _rows(sql, params=()):
    with connection() as conn:
        return [dict(x) for x in conn.execute(sql, params).fetchall()]


def _ingest_loop(stop: threading.Event):
    last_ingest_error = last_embedding_error = None
    while True:
        try:
            ingest_all()
        except Exception as exc:  # dashboard remains usable if one log is malformed/unreadable
            current = (type(exc), str(exc))
            if current != last_ingest_error:
                log.exception("ingest pass failed")
                last_ingest_error = current
        try:
            # Backfill writes only memory_embeddings; it may lazily load the local model.
            with connection() as conn:
                ensure_embeddings(conn)
        except Exception as exc:
            current = (type(exc), str(exc))
            if current != last_embedding_error:
                log.exception("embedding backfill failed")
                last_embedding_error = current
        if stop.wait(INGEST_EVERY_S):
            return


class AdoPulseQueue:
    """Small in-process cursor for ADO refresh notifications on the shared SSE stream."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cursor = 0
        self.events: list[tuple[int, dict]] = []

    def emit(self, event: dict):
        with self.lock:
            self.cursor += 1
            self.events.append((self.cursor, event))
            self.events = self.events[-100:]

    def since(self, cursor: int) -> tuple[list[dict], int]:
        with self.lock:
            return [event for number, event in self.events if number > cursor], self.cursor


class AdoCache:
    """ADO snapshot cache; unconfigured states reset the change baseline."""

    def __init__(self, client: AdoClient | None = None, on_waiting_added=None):
        self.client = client or AdoClient()
        self.on_waiting_added = on_waiting_added
        self.lock = threading.Lock()
        self.value: dict | None = None
        self.had_successful_refresh = False
        self.changes: list[dict] = []
        self.change_seq = 0
        self.last_config_error: str | None = None
        self.last_token_error: str | None = None
        self.configured_failure_types: set[type[BaseException]] = set()
        # The journal lives in memory; a new id per panel run tells browsers their seen seq is from an older run.
        self.journal_id = secrets.token_hex(4)

    @staticmethod
    def _waiting_ids(snapshot: dict | None) -> set[int | str]:
        if not snapshot:
            return set()
        names = ("to_review", "my_approved_not_completed", "my_changes_requested")
        return {pr["id"] for name in names for pr in snapshot.get(name, []) if pr.get("id") is not None}

    @staticmethod
    def _by_id(snapshot: dict, name: str) -> dict[int | str, dict]:
        return {item["id"]: item for item in snapshot.get(name, []) if item.get("id") is not None}

    @classmethod
    def _changes(cls, old: dict, fresh: dict) -> list[str]:
        changes = []
        old_tasks, new_tasks = cls._by_id(old, "open_tasks"), cls._by_id(fresh, "open_tasks")
        for task_id, task in new_tasks.items():
            if task_id not in old_tasks:
                changes.append(f"Task #{task_id} {task.get('title') or 'Untitled work item'}: new, {task.get('state') or ''}")
            elif task.get("state") != old_tasks[task_id].get("state"):
                changes.append(f"Task #{task_id} {task.get('title') or 'Untitled work item'}: {old_tasks[task_id].get('state') or ''} → {task.get('state') or ''}")
        for task_id, task in old_tasks.items():
            if task_id not in new_tasks:
                changes.append(f"Task #{task_id} {task.get('title') or 'Untitled work item'}: no longer open")

        labels = {"to_review": "to review", "my_approved_not_completed": "approved, not completed",
                  "my_changes_requested": "changes requested", "my_waiting_on_others": "waiting on others"}
        old_prs = {pr_id: (name, pr) for name in labels for pr_id, pr in cls._by_id(old, name).items()}
        new_prs = {pr_id: (name, pr) for name in labels for pr_id, pr in cls._by_id(fresh, name).items()}
        for pr_id, (name, pr) in new_prs.items():
            old_item = old_prs.get(pr_id)
            if old_item is None or old_item[0] != name:
                changes.append(f"PR !{pr_id} {pr.get('title') or 'Untitled pull request'}: now in {labels[name]}")
            if name == "my_waiting_on_others" and old_item and old_item[0] == name and set(pr.get("pending_reviewers", [])) != set(old_item[1].get("pending_reviewers", [])):
                reviewers = pr.get("pending_reviewers", [])
                changes.append(f"PR !{pr_id} {pr.get('title') or 'Untitled pull request'}: pending reviewers now {', '.join(reviewers) if reviewers else 'none'}")
        for pr_id, (_, pr) in old_prs.items():
            if pr_id not in new_prs:
                changes.append(f"PR !{pr_id} {pr.get('title') or 'Untitled pull request'}: no longer listed")
        return changes

    def get_changes(self, since: int = 0) -> tuple[int, list[dict]]:
        with self.lock:
            return self.change_seq, [copy.deepcopy(change) for change in reversed(self.changes) if change["seq"] > since]

    def _configured(self) -> tuple[bool, str | None]:
        try:
            return bool(self.client.configured()), None
        except Exception as exc:
            if type(exc) not in self.configured_failure_types:
                log.exception("Azure DevOps configuration check failed (%s)", type(exc).__name__)
                self.configured_failure_types.add(type(exc))
            return False, f"configuration check failed ({type(exc).__name__})"

    def get(self) -> dict:
        with self.lock:
            if self.value is not None:
                return copy.deepcopy(self.value)
        # Do not retain or display credentials; a token and valid names control this state.
        configured, config_error = self._configured()
        snapshot = empty_snapshot(configured=configured, config_error=config_error)
        snapshot["pending"] = True
        return snapshot

    def refresh(self) -> dict:
        try:
            return self._refresh()
        except Exception as exc:
            log.error("Unexpected Azure DevOps refresh error (%s)", type(exc).__name__, exc_info=True)
            self.unexpected_loop_error(exc)
            with self.lock:
                return copy.deepcopy(self.value)

    def _refresh(self) -> dict:
        try:
            fresh = self.client.snapshot()
        except AdoError as exc:
            log.warning("Azure DevOps refresh failed: %s", exc)
            with self.lock:
                return self._failed(str(exc))
        except Exception as exc:
            log.error("Unexpected Azure DevOps refresh error (%s)", type(exc).__name__, exc_info=True)
            with self.lock:
                return self._failed(f"unexpected error ({type(exc).__name__})")

        reason = fresh.get("config_error")
        if reason and reason != self.last_config_error:
            log.warning("Azure DevOps config problem: %s", reason)
        self.last_config_error = reason
        token_error = fresh.get("token_error")
        if token_error and token_error != self.last_token_error:
            log.warning("Azure DevOps token problem: %s", token_error)
        self.last_token_error = token_error

        with self.lock:
            if reason or not fresh.get("configured"):
                self.value = fresh
                self.had_successful_refresh = False
                return copy.deepcopy(fresh)
            # A different organization/project is new data, not a change: start a fresh baseline.
            had_successful_refresh = self.had_successful_refresh and (self.value or {}).get("scope") == fresh.get("scope")
            previous = self.value
            old_ids = self._waiting_ids(self.value)
            self.value = fresh
            self.had_successful_refresh = True
            if had_successful_refresh and previous is not None:
                at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                for text in self._changes(previous, fresh):
                    self.change_seq += 1
                    self.changes.append({"seq": self.change_seq, "at": at, "text": text})
                self.changes = self.changes[-100:]
            added = self._waiting_ids(fresh) - old_ids if had_successful_refresh else set()
            result = copy.deepcopy(fresh)
        # Initial data after startup or an unconfigured interval creates no alert.
        if added and self.on_waiting_added:
            for pr_id in added:
                self.on_waiting_added({"type": "ado", "id": pr_id})
        return result

    def _failed(self, error: str) -> dict:
        """Keep the last configured data marked stale; never re-show an old 'not configured' state."""
        if self.value is not None and self.value.get("configured"):
            snapshot = copy.deepcopy(self.value)
        else:
            # snapshot() only fails once a token and config exist, so the error is the news here.
            configured, config_error = self._configured()
            snapshot = empty_snapshot(configured=configured, config_error=config_error)
        snapshot["stale"] = True
        snapshot["pending"] = False
        snapshot["error"] = error
        self.value = snapshot
        return copy.deepcopy(snapshot)

    def unexpected_loop_error(self, exc: BaseException) -> None:
        with self.lock:
            self._failed(f"unexpected refresh loop error ({type(exc).__name__})")


_ado_pulses = AdoPulseQueue()
_ado_cache = AdoCache(on_waiting_added=_ado_pulses.emit)


def _ado_loop(stop: threading.Event):
    while True:
        try:
            _ado_cache.refresh()
        except Exception as exc:
            log.exception("Azure DevOps refresh loop error")
            if hasattr(_ado_cache, "unexpected_loop_error"):
                _ado_cache.unexpected_loop_error(exc)
        if stop.wait(ADO_REFRESH_S):
            return


class StateCache:
    """One snapshot shared by every request/stream client, rebuilt at most every STATE_TTL_S."""

    def __init__(self):
        self.lock = threading.Lock()
        self.value: dict | None = None
        self.at = 0.0

    def get(self) -> dict:
        with self.lock:
            if self.value is None or time.monotonic() - self.at > STATE_TTL_S:
                with connection() as conn:
                    self.value = live.build_state(conn)
                self.at = time.monotonic()
            return self.value


_cache = StateCache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    thread = threading.Thread(target=_ingest_loop, args=(stop,), daemon=True)
    ado_thread = threading.Thread(target=_ado_loop, args=(stop,), daemon=True)
    thread.start()
    ado_thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
        ado_thread.join(timeout=1)


app = FastAPI(title="devmem", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def render(request: Request, name: str, **context):
    context.setdefault("active", name.removesuffix(".html"))
    return templates.TemplateResponse(request, name, context)


# ---------------------------------------------------------------- live API

@app.get("/api/state")
def api_state():
    return _cache.get()


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"


def _pulses(since: dict) -> tuple[list, dict]:
    with connection() as conn:
        return live.pulses_since(conn, since)


@app.get("/api/stream")
async def api_stream(request: Request):
    async def wait_for_next_update() -> bool:
        """Wait in short slices so server shutdown does not wait for an SSE client."""
        remaining = STREAM_EVERY_S
        while remaining > 0:
            if _shutdown_event.is_set() or await request.is_disconnected():
                return False
            pause = min(0.5, remaining)
            started = time.monotonic()
            await asyncio.sleep(pause)
            remaining -= time.monotonic() - started
        return not _shutdown_event.is_set() and not await request.is_disconnected()

    async def events():
        snapshot = await asyncio.to_thread(_cache.get)
        since = dict(snapshot["cursor"])  # no replay of history: pulses start from now
        ado_cursor = _ado_pulses.since(0)[1]
        sent_sig = snapshot["graph_sig"]
        yield "retry: 3000\n\n" + _sse("state", snapshot)
        while not _shutdown_event.is_set() and not await request.is_disconnected():
            if not await wait_for_next_update():
                return
            try:
                pulses, since = await asyncio.to_thread(_pulses, since)
                snapshot = await asyncio.to_thread(_cache.get)
                ado_pulses, ado_cursor = _ado_pulses.since(ado_cursor)
            except Exception:
                log.exception("stream update failed")
                yield ": error\n\n"
                continue
            if pulses or ado_pulses:
                yield _sse("pulse", pulses + ado_pulses)
            # Diff-lite: the graph (the bulk of the payload) is only re-sent when its nodes change.
            body = dict(snapshot)
            if snapshot["graph_sig"] == sent_sig:
                body.pop("graph")
            sent_sig = snapshot["graph_sig"]
            yield _sse("state", body)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- pages

@app.get("/")
def brain(request: Request):
    return render(request, "brain.html", active="brain")


@app.get("/ado")
def ado(request: Request):
    return render(request, "ado.html", snapshot=_ado_cache.get())


@app.post("/ado/refresh")
def ado_refresh():
    _ado_cache.refresh()
    return RedirectResponse("/ado", 303)


@app.get("/api/ado")
def api_ado():
    return JSONResponse(_ado_cache.get())


@app.get("/api/ado/changes")
def api_ado_changes(since: int = 0):
    latest_seq, changes = _ado_cache.get_changes(max(0, since))
    return {"journal_id": _ado_cache.journal_id, "latest_seq": latest_seq, "changes": changes}


@app.get("/overview")
def overview(request: Request):
    totals = _rows("""SELECT agent,
      SUM(CASE WHEN datetime(ts) >= datetime('now','localtime','start of day','utc') THEN input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) today,
      SUM(CASE WHEN datetime(ts) >= datetime('now','-7 days') THEN input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) week,
      SUM(CASE WHEN datetime(ts) >= datetime('now','-30 days') THEN input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) month
      FROM usage_events GROUP BY agent""")
    daily = _rows("""SELECT date(ts,'localtime') day, agent,
      SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) tokens
      FROM usage_events WHERE datetime(ts) >= datetime('now','-30 days') GROUP BY day, agent ORDER BY day""")
    projects = _rows("""SELECT COALESCE(project,'Global') project,
      SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) tokens
      FROM usage_events GROUP BY project ORDER BY tokens DESC LIMIT 10""")
    with connection() as conn:
        limits = live.limits(conn)
    return render(request, "overview.html", totals=totals, daily=daily, projects=projects, limits=limits)


@app.get("/projects")
def projects(request: Request):
    rows = _rows("""WITH u AS (SELECT project,SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) tokens,
        COUNT(DISTINCT session_id) sessions,MAX(ts) activity FROM usage_events GROUP BY project),
      m AS (SELECT project,COUNT(*) memories,MAX(updated_at) activity FROM memories GROUP BY project),
      keys AS (SELECT project FROM u UNION SELECT project FROM m)
      SELECT COALESCE(keys.project,'Global') project,COALESCE(u.tokens,0) tokens,COALESCE(u.sessions,0) sessions,
        COALESCE(m.memories,0) memories,COALESCE(u.activity,m.activity) activity
      FROM keys LEFT JOIN u ON u.project IS keys.project LEFT JOIN m ON m.project IS keys.project
      ORDER BY datetime(COALESCE(u.activity,m.activity)) DESC""")
    return render(request, "projects.html", rows=rows)


@app.get("/projects/detail")
def project_detail(request: Request, path: str):
    return render(request, "project.html", active="projects", project=path,
                  events=_rows("SELECT * FROM usage_events WHERE project=? ORDER BY datetime(ts) DESC LIMIT 200", (path,)),
                  activity=_rows("SELECT * FROM activity_events WHERE project=? ORDER BY datetime(ts) DESC LIMIT 100", (path,)),
                  memories=_rows("SELECT * FROM memories WHERE project=? ORDER BY updated_at DESC", (path,)))


@app.get("/sessions")
def sessions(request: Request):
    rows = _rows("""SELECT session_id,agent,project,model,MIN(ts) start,MAX(ts) end,COUNT(*) turns,SUM(input_tokens) input,
        SUM(output_tokens) output,SUM(cache_read_tokens) cache FROM usage_events
        GROUP BY session_id,agent,project,model ORDER BY datetime(end) DESC LIMIT 500""")
    return render(request, "sessions.html", rows=rows)


@app.get("/sessions/{session_id}")
def session_detail(request: Request, session_id: str):
    return render(request, "session.html", active="sessions", session_id=session_id,
                  events=_rows("SELECT * FROM usage_events WHERE session_id=? ORDER BY datetime(ts)", (session_id,)),
                  activity=_rows("SELECT * FROM activity_events WHERE session_id=? ORDER BY datetime(ts) DESC LIMIT 200", (session_id,)))


@app.get("/agents")
def agents(request: Request):
    clients = _rows("""SELECT a.*,COUNT(t.id) tool_count FROM agent_clients a LEFT JOIN tool_calls t ON t.client_id=a.id
        GROUP BY a.id ORDER BY a.last_seen_at DESC""")
    for client in clients:
        client["connected"] = live.client_connected(client)
    calls = _rows("SELECT t.*,a.client_name FROM tool_calls t JOIN agent_clients a ON a.id=t.client_id ORDER BY t.at DESC LIMIT 100")
    return render(request, "agents.html", clients=clients, calls=calls)


@app.get("/memory")
def memory(request: Request, q: str = "", kind: str = ""):
    embedder_status = None
    if q and fts_query(q):
        with connection() as conn:
            matches = hybrid_search(conn, q, where_sql="1=1", kind=kind or None, limit=200)
            ids = [match["id"] for match in matches]
            full_rows = conn.execute(
                f"SELECT * FROM memories WHERE id IN ({','.join('?' for _ in ids)})", ids
            ).fetchall() if ids else []
            by_id = {row["id"]: dict(row) for row in full_rows}
            # Hydrate fields used by the edit form without changing hybrid search's
            # compact public/MCP projection, and retain its rank and annotations.
            rows = [{**by_id[match["id"]], "match": match["match"], "snippet": match.get("snippet")}
                    for match in matches if match["id"] in by_id]
        status = getattr(get_embedder(), "status", lambda: {})()
        if status and (not status.get("enabled") or status.get("last_error")):
            embedder_status = status
    else:
        sql, params = "SELECT * FROM memories", []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        rows = _rows(sql + " ORDER BY updated_at DESC LIMIT 200", params)
    return render(request, "memory.html", rows=rows, q=q, kind=kind, embedder_status=embedder_status,
                  counts=_rows("SELECT kind,COUNT(*) count FROM memories GROUP BY kind"))


@app.post("/memory")
def create_memory(content: str = Form(...), title: str = Form(""), kind: str = Form("note"), tags: str = Form(""), project: str = Form("")):
    with connection() as conn:
        conn.execute("INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at,source_agent) VALUES(?,?,?,?,?,?,?,?)",
                     (project or None, kind, title or content[:80], content, tags, now(), now(), "panel"))
    return RedirectResponse("/memory", 303)


@app.post("/memory/{id}")
def edit_memory(id: int, content: str = Form(...), title: str = Form(...), kind: str = Form(...), tags: str = Form("")):
    with connection() as conn:
        conn.execute("UPDATE memories SET content=?,title=?,kind=?,tags=?,updated_at=? WHERE id=?", (content, title, kind, tags, now(), id))
    return RedirectResponse("/memory", 303)


@app.post("/memory/{id}/delete")
def delete_memory(id: int):
    with connection() as conn:
        conn.execute("DELETE FROM memories WHERE id=?", (id,))
    return RedirectResponse("/memory", 303)
