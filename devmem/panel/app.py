from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..db import connection, fts_query, now
from ..ingest.runner import ingest_all

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

def _rows(sql, params=()):
    with connection() as conn: return [dict(x) for x in conn.execute(sql, params).fetchall()]

def _ingest_loop(stop: threading.Event):
    while not stop.wait(60):
        try: ingest_all()
        except Exception: pass  # dashboard remains usable if one log is malformed/unreadable

@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event(); thread = threading.Thread(target=_ingest_loop, args=(stop,), daemon=True); thread.start()
    try: yield
    finally: stop.set(); thread.join(timeout=1)

app = FastAPI(title="devmem", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

def render(request: Request, name: str, **context): return templates.TemplateResponse(request, name, context)

@app.get("/")
def overview(request: Request):
    totals = _rows("""SELECT agent,
      SUM(CASE WHEN datetime(ts) >= datetime('now','-1 day') THEN input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) today,
      SUM(CASE WHEN datetime(ts) >= datetime('now','-7 days') THEN input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) week,
      SUM(CASE WHEN datetime(ts) >= datetime('now','-30 days') THEN input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) month
      FROM usage_events GROUP BY agent""")
    daily = _rows("SELECT substr(ts,1,10) day,agent,SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) tokens FROM usage_events WHERE datetime(ts) >= datetime('now','-30 days') GROUP BY day,agent ORDER BY day")
    projects = _rows("SELECT COALESCE(project,'Global') project,SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) tokens FROM usage_events GROUP BY project ORDER BY tokens DESC LIMIT 10")
    limits = _rows("SELECT * FROM rate_limits WHERE agent='codex' ORDER BY ts DESC LIMIT 2")
    return render(request,"overview.html", totals=totals,daily=daily,projects=projects,limits=limits)

@app.get("/projects")
def projects(request: Request):
    rows = _rows("""WITH u AS (SELECT project,SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) tokens,COUNT(DISTINCT session_id) sessions,MAX(ts) activity FROM usage_events GROUP BY project), m AS (SELECT project,COUNT(*) memories,MAX(updated_at) activity FROM memories GROUP BY project), keys AS (SELECT project FROM u UNION SELECT project FROM m) SELECT COALESCE(keys.project,'Global') project,COALESCE(u.tokens,0) tokens,COALESCE(u.sessions,0) sessions,COALESCE(m.memories,0) memories,COALESCE(u.activity,m.activity) activity FROM keys LEFT JOIN u ON u.project IS keys.project LEFT JOIN m ON m.project IS keys.project ORDER BY activity DESC""")
    return render(request,"projects.html",rows=rows)

@app.get("/projects/detail")
def project_detail(request: Request, path: str):
    return render(request,"project.html", project=path, events=_rows("SELECT * FROM usage_events WHERE project=? ORDER BY ts DESC LIMIT 200",(path,)), memories=_rows("SELECT * FROM memories WHERE project=? ORDER BY updated_at DESC",(path,)))

@app.get("/sessions")
def sessions(request: Request):
    rows=_rows("SELECT session_id,agent,project,model,MIN(ts) start,MAX(ts) end,COUNT(*) turns,SUM(input_tokens) input,SUM(output_tokens) output,SUM(cache_read_tokens) cache FROM usage_events GROUP BY session_id,agent,project,model ORDER BY end DESC")
    return render(request,"sessions.html",rows=rows)

@app.get("/sessions/{session_id}")
def session_detail(request: Request, session_id: str): return render(request,"session.html",session_id=session_id,events=_rows("SELECT * FROM usage_events WHERE session_id=? ORDER BY ts",(session_id,)))

@app.get("/agents")
def agents(request: Request):
    clients=_rows("SELECT a.*,COUNT(t.id) tool_count FROM agent_clients a LEFT JOIN tool_calls t ON t.client_id=a.id GROUP BY a.id ORDER BY a.last_seen_at DESC")
    for client in clients:
        try:
            os.kill(int(client["pid"]), 0)
            client["connected"] = client["ended_at"] is None and (time.time() - __import__('datetime').datetime.fromisoformat(client["last_seen_at"]).timestamp() < 300)
        except (OSError, TypeError, ValueError):
            client["connected"] = False
    calls=_rows("SELECT t.*,a.client_name FROM tool_calls t JOIN agent_clients a ON a.id=t.client_id ORDER BY t.at DESC LIMIT 100")
    return render(request,"agents.html",clients=clients,calls=calls)

@app.get("/memory")
def memory(request: Request, q: str = "", kind: str = ""):
    sql="SELECT * FROM memories"; params=[]
    if q and fts_query(q): sql="SELECT m.* FROM memories_fts JOIN memories m ON m.id=memories_fts.rowid WHERE memories_fts MATCH ?";params=[fts_query(q)]
    if kind: sql += (" AND" if " WHERE " in sql else " WHERE")+" kind=?";params.append(kind)
    rows=_rows(sql+" ORDER BY updated_at DESC LIMIT 200",params)
    return render(request,"memory.html",rows=rows,q=q,kind=kind,counts=_rows("SELECT kind,COUNT(*) count FROM memories GROUP BY kind"))

@app.post("/memory")
def create_memory(content: str=Form(...),title: str=Form(""),kind: str=Form("note"),tags: str=Form(""),project: str=Form("")):
    with connection() as conn: conn.execute("INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at,source_agent) VALUES(?,?,?,?,?,?,?,?)",(project or None,kind,title or content[:80],content,tags,now(),now(),"panel"))
    return RedirectResponse("/memory",303)

@app.post("/memory/{id}")
def edit_memory(id:int,content:str=Form(...),title:str=Form(...),kind:str=Form(...),tags:str=Form("")):
    with connection() as conn: conn.execute("UPDATE memories SET content=?,title=?,kind=?,tags=?,updated_at=? WHERE id=?",(content,title,kind,tags,now(),id))
    return RedirectResponse("/memory",303)

@app.post("/memory/{id}/delete")
def delete_memory(id:int):
    with connection() as conn: conn.execute("DELETE FROM memories WHERE id=?",(id,))
    return RedirectResponse("/memory",303)
