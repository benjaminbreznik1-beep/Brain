from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..db import connection, fts_query, now
from ..ingest.runner import ingest_all
from . import state as live

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
log = logging.getLogger("devmem.panel")

INGEST_EVERY_S = 5
STREAM_EVERY_S = 2.5
STATE_TTL_S = 2.0


def _rows(sql, params=()):
    with connection() as conn:
        return [dict(x) for x in conn.execute(sql, params).fetchall()]


def _ingest_loop(stop: threading.Event):
    while True:
        try:
            ingest_all()
        except Exception:  # dashboard remains usable if one log is malformed/unreadable
            log.exception("ingest pass failed")
        if stop.wait(INGEST_EVERY_S):
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
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


app = FastAPI(title="devmem", lifespan=lifespan)
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
    async def events():
        snapshot = await asyncio.to_thread(_cache.get)
        since = dict(snapshot["cursor"])  # no replay of history: pulses start from now
        sent_sig = snapshot["graph_sig"]
        yield "retry: 3000\n\n" + _sse("state", snapshot)
        while not await request.is_disconnected():
            await asyncio.sleep(STREAM_EVERY_S)
            try:
                pulses, since = await asyncio.to_thread(_pulses, since)
                snapshot = await asyncio.to_thread(_cache.get)
            except Exception:
                log.exception("stream update failed")
                yield ": error\n\n"
                continue
            if pulses:
                yield _sse("pulse", pulses)
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


@app.get("/overview")
def overview(request: Request):
    totals = _rows("""SELECT agent,
      SUM(CASE WHEN datetime(ts) >= datetime('now','-1 day') THEN input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) today,
      SUM(CASE WHEN datetime(ts) >= datetime('now','-7 days') THEN input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) week,
      SUM(CASE WHEN datetime(ts) >= datetime('now','-30 days') THEN input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) month
      FROM usage_events GROUP BY agent""")
    daily = _rows("""SELECT substr(datetime(ts),1,10) day, agent,
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
    sql = "SELECT * FROM memories"
    params = []
    if q and fts_query(q):
        sql = "SELECT m.* FROM memories_fts JOIN memories m ON m.id=memories_fts.rowid WHERE memories_fts MATCH ?"
        params = [fts_query(q)]
    if kind:
        sql += (" AND" if " WHERE " in sql else " WHERE") + " kind=?"
        params.append(kind)
    rows = _rows(sql + " ORDER BY updated_at DESC LIMIT 200", params)
    return render(request, "memory.html", rows=rows, q=q, kind=kind,
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
