from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import messages as msgs
from ..db import audit_memories, connection, ensure_embeddings, fts_query, hybrid_search, is_stale, not_superseded_sql, now, parse_task_board, validate_content, validate_review_after
from ..embed import get_embedder
from ..ingest.runner import ingest_all
from ..ado import AdoClient, AdoError, empty_snapshot
from . import state as live

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def asset(path: str) -> str:
    """Static URL tagged with the file's mtime: an edited script is fetched again instead of served from the browser cache."""
    try: version = int((HERE / "static" / path).stat().st_mtime)
    except OSError: version = 0
    return f"/static/{path}?v={version}"


templates.env.globals["asset"] = asset


def when(value, seconds: bool = False) -> str:
    """An ISO timestamp as local time with its zone ("30 Oct 07:36 CET"); anything else is shown as given."""
    if not value or not isinstance(value, str): return value or ""
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
    try: parsed = datetime.fromisoformat(text)
    except ValueError: return value
    if "T" not in text and " " not in text: return f"{parsed.day} {parsed:%b %Y}"
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone()
    return f"{local.day} {local:%b %H:%M{':%S' if seconds else ''} %Z}"


def window_label(limit) -> str:
    """A usage-limit window named the way the Brain gauges name it: 5h, weekly, weekly opus, 2d."""
    label, minutes = limit.get("label") or "", limit.get("window_minutes")
    named = {"five_hour": "5h", "seven_day": "weekly"}
    if label in named: return named[label]
    if label.startswith("seven_day_"): return "weekly " + label.removeprefix("seven_day_").replace("_", " ")
    base = "5h" if minutes == 300 else "weekly" if minutes == 10080 else (f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{round(minutes / 60)}h") if minutes else label
    prefix = label.split(":")[0] + " " if ":" in label[1:] else ""
    return prefix + base


templates.env.filters["when"] = when
templates.env.filters["window_label"] = window_label
log = logging.getLogger("devmem.panel")

INGEST_EVERY_S = 5
PRUNE_EVERY_S = 3600
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
    pruned_at = 0.0
    while True:
        if time.monotonic() - pruned_at > PRUNE_EVERY_S:  # message retention works even when maintenance is disabled
            try:
                from ..maintenance import prune_messages
                prune_messages()
            except Exception:
                log.exception("message pruning failed")
            finally:
                pruned_at = time.monotonic()
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


def _git_sync_loop(stop: threading.Event):
    while not stop.is_set():
        try:
            from .. import git_sync
            cfg = git_sync.config()
            interval = max(1, int(cfg.get("interval_s", 300)))
            if cfg.get("enabled"): git_sync.sync()
        except Exception:
            log.exception("GitHub sync loop error")
            interval = 300
        if stop.wait(interval): return


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
    from ..dispatch import loop as dispatch_loop
    stop = threading.Event()
    thread = threading.Thread(target=_ingest_loop, args=(stop,), daemon=True)
    ado_thread = threading.Thread(target=_ado_loop, args=(stop,), daemon=True)
    git_thread = threading.Thread(target=_git_sync_loop, args=(stop,), daemon=True)
    dispatch_thread = threading.Thread(target=dispatch_loop, args=(stop,), daemon=True)
    thread.start()
    ado_thread.start()
    git_thread.start()
    dispatch_thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
        ado_thread.join(timeout=1)
        git_thread.join(timeout=1)
        dispatch_thread.join(timeout=5)


app = FastAPI(title="devmem", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """fix(2026-10-28): refuse cross-site writes. Any page in the browser could otherwise auto-submit a forged user reply that a waiting
    agent acts on, or revert maintenance. Browsers send Origin / Sec-Fetch-Site on every POST; scripts without them are not browsers."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        site, origin = request.headers.get("sec-fetch-site"), request.headers.get("origin")
        own = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if (site is not None and site not in ("same-origin", "none")) or (origin is not None and origin != own):
            return PlainTextResponse("cross-site request refused", 403)
    return await call_next(request)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def render(request: Request, name: str, **context):
    context.setdefault("active", name.removesuffix(".html"))
    from ..maintenance import attention
    with connection() as conn:
        state, messages_badge = attention(conn), msgs.badge(conn)
    context.setdefault("maintenance_count", state["unseen"] + (1 if state["failing_since"] else 0))
    context.setdefault("messages_badge", messages_badge)
    return templates.TemplateResponse(request, name, context)


# ---------------------------------------------------------------- live API

@app.get("/api/state")
def api_state():
    return _cache.get()


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"


def _messages() -> dict:
    with connection() as conn:
        return msgs.snapshot(conn)


def _pulses(since: dict) -> tuple[list, dict]:
    with connection() as conn:
        return live.pulses_since(conn, since)


@app.get("/api/stream")
async def api_stream(request: Request, only: str = ""):
    messages_only = only == "messages"  # the chat page needs no state, graph or pulses
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

    async def feed_or_none():
        try:
            return await asyncio.to_thread(_messages)
        except Exception:  # fix(2026-10-28): a messages failure must not take state and pulses (or the stream) down with it
            log.exception("messages update failed")
            return None

    async def events():
        sent_sig = since = ado_cursor = None
        if not messages_only:
            snapshot = await asyncio.to_thread(_cache.get)
            since = dict(snapshot["cursor"])  # no replay of history: pulses start from now
            ado_cursor = _ado_pulses.since(0)[1]
            sent_sig = snapshot["graph_sig"]
        sent_messages = await feed_or_none()
        yield "retry: 3000\n\n" + ("" if messages_only else _sse("state", snapshot)) + (_sse("messages", sent_messages) if sent_messages else "")
        while not _shutdown_event.is_set() and not await request.is_disconnected():
            if not await wait_for_next_update():
                return
            if not messages_only:
                try:
                    pulses, since = await asyncio.to_thread(_pulses, since)
                    snapshot = await asyncio.to_thread(_cache.get)
                    ado_pulses, ado_cursor = _ado_pulses.since(ado_cursor)
                except Exception:
                    log.exception("stream update failed")
                    yield ": error\n\n"
                else:
                    if pulses or ado_pulses:
                        yield _sse("pulse", pulses + ado_pulses)
                    # Diff-lite: the graph (the bulk of the payload) is only re-sent when its nodes change.
                    body = dict(snapshot)
                    if snapshot["graph_sig"] == sent_sig:
                        body.pop("graph")
                    sent_sig = snapshot["graph_sig"]
                    yield _sse("state", body)
            feed = await feed_or_none()
            if feed is not None and feed != sent_messages:
                sent_messages = feed
                yield _sse("messages", feed)
            elif messages_only:
                yield ": keepalive\n\n"

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
        limits, tokens = live.limits(conn), live.tokens(conn)
        boards = []
        for row in conn.execute(f"SELECT id,title,content,tags FROM memories m WHERE {not_superseded_sql()}"):
            if "board" not in re.split(r"[\s,]+", (row["tags"] or "").lower()): continue
            entries, status_as_of = parse_task_board(row["content"])
            boards.append({"id": row["id"], "title": row["title"], "entries": entries, "status_as_of": status_as_of})
    return render(request, "overview.html", totals=totals, daily=daily, projects=projects, limits=limits, tokens=tokens, boards=boards)


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
    for row in rows:
        row["stale"] = is_stale(row.get("review_after"))
        from ..git_sync import in_scope
        row["synced"] = in_scope(row)
    return render(request, "memory.html", rows=rows, q=q, kind=kind, embedder_status=embedder_status,
                  counts=_rows("SELECT kind,COUNT(*) count FROM memories GROUP BY kind"))


@app.get("/memory/{id}")
def memory_detail(request: Request, id: int):
    row = _rows("SELECT * FROM memories WHERE id=?", (id,))
    if not row: raise HTTPException(404, "memory not found")
    row[0]["stale"] = is_stale(row[0].get("review_after"))
    from ..git_sync import in_scope
    row[0]["synced"] = in_scope(row[0])
    return render(request, "memory.html", rows=row, q="", kind="", embedder_status=None,
                  counts=_rows("SELECT kind,COUNT(*) count FROM memories GROUP BY kind"))


@app.post("/memory")
def create_memory(content: str = Form(...), title: str = Form(""), kind: str = Form("note"), tags: str = Form(""), project: str = Form(""), review_after: str = Form("")):
    try:
        validate_content(content); review_after = validate_review_after(review_after)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with connection() as conn:
        conn.execute("INSERT INTO memories(project,kind,title,content,tags,review_after,created_at,updated_at,source_agent) VALUES(?,?,?,?,?,?,?,?,?)",
                     (project or None, kind, title or content[:80], content, tags, review_after, now(), now(), "panel"))
    return RedirectResponse("/memory", 303)


@app.post("/memory/{id}")
def edit_memory(id: int, content: str = Form(...), title: str = Form(...), kind: str = Form(...), tags: str = Form(""), review_after: str = Form("")):
    try:
        validate_content(content); review_after = validate_review_after(review_after)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with connection() as conn:
        conn.execute("UPDATE memories SET content=?,title=?,kind=?,tags=?,review_after=?,updated_at=? WHERE id=?", (content, title, kind, tags, review_after, now(), id))
    return RedirectResponse("/memory", 303)


@app.post("/memory/{id}/delete")
def delete_memory(id: int):
    with connection() as conn:
        conn.execute("DELETE FROM memories WHERE id=?", (id,))
    return RedirectResponse("/memory", 303)


@app.get("/audit")
def audit(request: Request):
    with connection() as conn:
        rows = audit_memories(conn)
    return render(request, "audit.html", rows=rows)


@app.get("/maintenance")
def maintenance(request: Request):
    from ..maintenance import OPEN_FLAG_SQL, attention
    with connection() as conn:
        failing = attention(conn)["failing_since"]
        runs = [dict(row) for row in conn.execute("SELECT * FROM maintenance_runs ORDER BY id DESC LIMIT 50")]
        changes = [dict(row) for row in conn.execute(f"""SELECT c.*, r.status run_status, ({OPEN_FLAG_SQL}) open_flag
            FROM maintenance_changes c LEFT JOIN maintenance_runs r ON r.id=c.run_id WHERE c.blocked_by IS NOT 'no_change' ORDER BY c.id DESC LIMIT 300""")]
        # Rendering the page is "seeing" it: the nav badge only counts what arrived since.
        conn.execute("UPDATE maintenance_changes SET seen_at=? WHERE seen_at IS NULL", (now(),))
    for change in changes:
        try: change["evidence_items"] = json.loads(change["evidence"])
        except (TypeError, ValueError): change["evidence_items"] = None
        if not isinstance(change["evidence_items"], list):  # fix(2026-10-28): say so instead of showing "no evidence"
            change["evidence_items"] = [{"ref": "corrupt evidence record", "result": "the stored evidence could not be read"}]
    from .. import git_sync
    try: git_status = git_sync.status()
    except ValueError as exc: git_status = {"enabled": False, "error": str(exc), "out_of_sync": []}
    return render(request, "maintenance.html", runs=runs, changes=changes, failing=failing, git_status=git_status, notice=request.query_params.get("notice"))


@app.post("/git/sync")
def git_sync_now():
    from .. import git_sync
    result = git_sync.sync()
    notice = result.get("errors") and "; ".join(result["errors"]) or "GitHub sync finished"
    return RedirectResponse("/maintenance?notice=" + quote(notice), 303)


@app.post("/maintenance/cleanup")
def maintenance_cleanup():
    from ..maintenance import cleanup
    result = cleanup()
    notice = result.get("line") or ("cleanup is already running" if result["status"] == "locked" else "cleanup finished")
    return RedirectResponse("/maintenance?notice=" + quote(notice), 303)


@app.post("/maintenance/{change_id}/revert")
def maintenance_revert(change_id: int):
    from ..maintenance import revert
    try:
        if not revert(change_id): raise HTTPException(404, "change cannot be reverted")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse("/maintenance", 303)


@app.post("/maintenance/{change_id}/dismiss")
def maintenance_dismiss(change_id: int):
    from ..maintenance import dismiss
    if not dismiss(change_id): raise HTTPException(404, "only an open flag can be dismissed")
    return RedirectResponse("/maintenance", 303)


# ---------------------------------------------------------------- messages

@app.get("/api/messages")
def api_messages():
    return _messages()


@app.get("/api/dispatch")
def api_dispatch():
    from ..dispatch import config
    cfg = config()
    with connection() as conn:
        jobs = [dict(row) for row in conn.execute("SELECT * FROM message_jobs ORDER BY id DESC LIMIT 20")]
    return {"enabled": cfg["enabled"], "error": cfg.get("error"), "jobs": jobs}


def _chat_filters(project: str, agent: str, kind: str, status: str, question: str) -> dict:
    return {"project": project, "agent": agent, "kind": kind if kind in msgs.KINDS else "",
            "status": status if status in ("open", "answered", "closed") else "", "question": bool(question)}


@app.get("/messages")
def messages_page(request: Request, chat: str = "", project: str = "", agent: str = "", kind: str = "", status: str = "", question: str = ""):
    """WhatsApp-style chats: the list on the left, the open chat on the right (stacked on narrow screens)."""
    from ..dispatch import config as dispatch_config
    filters = _chat_filters(project, agent, kind, status, question)
    with connection() as conn:
        rows, facets = msgs.chats(conn, **filters), msgs.facets(conn)
        current = msgs.chat(conn, chat) if chat else None
        if chat and not current: raise HTTPException(404, "chat not found")
        if current: msgs.mark_chat_read(conn, current)  # opening a chat is reading it
    if current:
        for row in rows:
            if row["key"] == current["key"]: row["unread"] = 0
    return render(request, "messages.html", active="messages", chats=rows, chat=current, facets=facets, filters=filters, kinds=msgs.KINDS,
                  dispatch=dispatch_config())


@app.get("/api/chats")
def api_chats(project: str = "", agent: str = "", kind: str = "", status: str = "", question: str = ""):
    with connection() as conn:
        return msgs.chats(conn, **_chat_filters(project, agent, kind, status, question))


@app.get("/api/chat")
def api_chat(key: str, read: int = 0):
    with connection() as conn:
        current = msgs.chat(conn, key)
        if not current: raise HTTPException(404, "chat not found")
        if read: msgs.mark_chat_read(conn, current)  # the open chat page reads what arrives live
    return current


@app.post("/messages/chat/send")
def chat_send(request: Request, key: str = Form(...), body: str = Form(...)):
    with connection() as conn:
        try:
            message_id = msgs.chat_post(conn, key, body)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if "application/json" in request.headers.get("accept", ""):
        return {"id": message_id}
    return RedirectResponse("/messages?chat=" + quote(key, safe=""), 303)


@app.get("/messages/thread/{thread_id}")
def message_thread_page(request: Request, thread_id: int):
    with connection() as conn:
        items = msgs.thread(conn, thread_id)
        if not items: raise HTTPException(404, "thread not found")
        agents = {aid: msgs.subagent(conn, aid) for aid in dict.fromkeys(m["agent_id"] for m in items if m["agent_id"])}
        # Opening a thread is reading it: unread and badge counts only cover what the user has not seen.
        msgs.mark_read(conn, [m["id"] for m in items], msgs.USER)
        chat_key = msgs.chat_keys(conn, msgs._roots(conn, [thread_id])).get(thread_id)
    return render(request, "message_thread.html", active="messages", items=items, root=items[0], agents=agents, chat_key=chat_key)


@app.post("/messages/thread/{thread_id}/reply")
def message_reply(thread_id: int, body: str = Form(...)):
    with connection() as conn:
        items = msgs.thread(conn, thread_id)
        if not items: raise HTTPException(404, "thread not found")
        # Answer the newest open question from an agent, otherwise continue after the newest message.
        target = next((m for m in reversed(items) if m["kind"] == "question" and m["status"] == "open" and m["sender_kind"] != "user"), items[-1])
        try:
            message_id = msgs.post(conn, body, sender=msgs.USER, sender_kind="user", to=target["sender"] if target["sender_kind"] != "user" else "*",
                                   reply_to=target["id"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        msgs.mark_read(conn, [message_id], msgs.USER)
        from ..dispatch import enqueue
        chat_key = msgs.chat_keys(conn, msgs._roots(conn, [items[0]["thread_id"]])).get(items[0]["thread_id"])
        enqueue(conn, message_id, chat_key or f"t:{items[0]['thread_id']}")
    return RedirectResponse(f"/messages/thread/{thread_id}", 303)


@app.post("/messages/thread/{thread_id}/close")
def message_close(thread_id: int):
    with connection() as conn: msgs.close(conn, thread_id)
    return RedirectResponse(f"/messages/thread/{thread_id}", 303)


@app.post("/messages/thread/{thread_id}/reopen")
def message_reopen(thread_id: int):
    with connection() as conn: msgs.reopen(conn, thread_id)
    return RedirectResponse(f"/messages/thread/{thread_id}", 303)


@app.get("/messages/agent/{agent_id}")
def subagent_page(request: Request, agent_id: str):
    with connection() as conn:
        agent = msgs.subagent(conn, agent_id)
    if not agent: raise HTTPException(404, "subagent not found")
    return render(request, "subagent.html", active="messages", agent=agent)
