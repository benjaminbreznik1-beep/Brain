"""Live snapshot (/api/state) and pulse (/api/stream) queries.

Timestamps in the DB are mixed ISO formats, so time windows always compare with datetime(ts).  A plain text
prefilter on the date (ts >= 'YYYY-MM-DD' a day earlier than needed) lets the ts indexes skip old rows first.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Any

from .. import messages
from ..db import SCHEMA_VERSION, parse_ts

STALE_AFTER_S = 6 * 3600
RECENT_SESSION_S = 600
CONNECTED_S = 300
PULSE_MAX_AGE_S = 600
MAX_PROJECTS = 12
MAX_MEMORIES = 250
FEED_LEN = 30
TOKEN_COLS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _now() -> datetime:
    return datetime.now(UTC)


def _prefilter(days: float) -> str:
    """Date-only lower bound, one day wider than the real window, valid for every ISO variant."""
    return (_now() - timedelta(days=days + 1)).strftime("%Y-%m-%d")


def _rows(conn, sql: str, params=()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _iso(value) -> str | None:
    parsed = parse_ts(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def basename(project: str | None) -> str:
    return PurePath(project).name or project if project else "global"


# ---------------------------------------------------------------- limits

def limits(conn) -> list[dict[str, Any]]:
    rows = _rows(conn, """SELECT * FROM (
        SELECT r.*, ROW_NUMBER() OVER (PARTITION BY agent,label ORDER BY datetime(ts) DESC, id DESC) rn
        FROM rate_limits r WHERE label IS NOT NULL) WHERE rn=1""")
    now = _now()
    out = []
    for r in rows:
        used = float(r["used_percent"]) if r["used_percent"] is not None else None
        resets = parse_ts(r["resets_at"])
        as_of = max(filter(None, (parse_ts(r["ts"]), parse_ts(r.get("observed_at")))), default=None)
        age = (now - as_of).total_seconds() if as_of else None
        out.append({
            "agent": r["agent"], "label": r["label"], "window_minutes": r["window_minutes"],
            "used_percent": used, "left_percent": None if used is None else max(0.0, round(100 - used, 1)),
            "resets_at": _iso(resets), "resets_in_s": int((resets - now).total_seconds()) if resets else None,
            "reset_passed": bool(resets and resets <= now),
            "as_of": _iso(as_of), "age_s": int(age) if age is not None else None,
            "stale": age is None or age > STALE_AFTER_S,
        })
    order = {"claude": 0, "codex": 1}
    out.sort(key=lambda x: (order.get(x["agent"], 9), x["window_minutes"] or 99999, x["label"]))
    return out


# ---------------------------------------------------------------- tokens

def _token_dict(row: dict[str, Any], prefix: str = "") -> dict[str, int]:
    d = {
        "input": int(row.get(prefix + "input") or 0), "output": int(row.get(prefix + "output") or 0),
        "cache_read": int(row.get(prefix + "cache_read") or 0), "cache_write": int(row.get(prefix + "cache_write") or 0),
    }
    d["total"] = sum(d.values())
    return d


def tokens(conn) -> dict[str, Any]:
    today_start = conn.execute("SELECT datetime('now','localtime','start of day','utc')").fetchone()[0]
    pre = _prefilter(7)
    sums = lambda cond, p: ", ".join(f"SUM(CASE WHEN {cond} THEN {c} ELSE 0 END) {p}{n}" for c, n in zip(TOKEN_COLS, ("input", "output", "cache_read", "cache_write")))
    per_agent = _rows(conn, f"""SELECT agent, {sums('datetime(ts) >= :today', 't_')}, {sums("datetime(ts) >= datetime('now','-7 days')", 'w_')}
        FROM usage_events WHERE ts >= :pre GROUP BY agent""", {"today": today_start, "pre": pre})
    models = _rows(conn, f"""SELECT agent, model, {sums('1', '')} FROM usage_events
        WHERE ts >= :pre AND datetime(ts) >= :today GROUP BY agent, model""", {"today": today_start, "pre": _prefilter(1)})
    hourly = _rows(conn, """SELECT agent, strftime('%Y-%m-%dT%H', datetime(ts)) hour,
        SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) total
        FROM usage_events WHERE ts >= ? AND datetime(ts) >= datetime('now','-24 hours') GROUP BY agent, hour""", (_prefilter(1),))
    now = _now().replace(minute=0, second=0, microsecond=0)
    hours = [(now - timedelta(hours=23 - i)).strftime("%Y-%m-%dT%H") for i in range(24)]
    spark: dict[str, list[int]] = {}
    for h in hourly:
        if h["hour"] in hours:
            spark.setdefault(h["agent"], [0] * 24)[hours.index(h["hour"])] = int(h["total"] or 0)
    today = {r["agent"]: _token_dict(r, "t_") for r in per_agent}
    week = {r["agent"]: _token_dict(r, "w_") for r in per_agent}
    model_rows = [{"agent": m["agent"], "model": m["model"], **_token_dict(m)} for m in models]
    model_rows.sort(key=lambda m: -m["total"])
    return {
        "today_start": _iso(today_start), "today": today, "week": week,
        "today_total": sum(v["total"] for v in today.values()), "week_total": sum(v["total"] for v in week.values()),
        "models_today": model_rows, "hourly": {"hours": [h + ":00:00Z" for h in hours], "series": spark},
    }


# ---------------------------------------------------------------- agents and sessions

def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def client_connected(client: dict[str, Any]) -> bool:
    """Same rule as the /agents page: not ended, seen in the last 5 minutes and PID alive (remote clients have no PID)."""
    seen = parse_ts(client.get("last_seen_at"))
    return (client.get("ended_at") is None and seen is not None
            and (_now() - seen).total_seconds() < CONNECTED_S and (bool(client.get("remote_client_id")) or _pid_alive(client.get("pid"))))


def agent_family(name: str | None) -> str:
    name = (name or "").lower()
    if "codex" in name:
        return "codex"
    if "claude" in name:
        return "claude"
    return name or "mcp"


def connected_clients(conn) -> list[dict[str, Any]]:
    rows = _rows(conn, """SELECT a.id, a.client_name, a.client_version, a.pid, a.project, a.started_at, a.last_seen_at, a.ended_at, a.remote_client_id,
        (SELECT tool FROM tool_calls t WHERE t.client_id=a.id ORDER BY t.id DESC LIMIT 1) last_tool
        FROM agent_clients a WHERE a.ended_at IS NULL ORDER BY a.id DESC LIMIT 100""")
    out = []
    for r in rows:
        if client_connected(r):
            remote = bool(r["remote_client_id"])
            out.append({"id": r["id"], "client_name": f"{r['client_name']} (remote)" if remote else r["client_name"], "family": agent_family(r["client_name"]),
                        "pid": r["pid"], "project": r["project"], "project_name": basename(r["project"]), "last_seen": _iso(r["last_seen_at"]),
                        "last_tool": r["last_tool"], "remote": remote})
    return out


def recent_sessions(conn) -> list[dict[str, Any]]:
    pre = _prefilter(1)
    window = f"-{RECENT_SESSION_S} seconds"
    acts = _rows(conn, """SELECT agent, session_id, project, ts, name, summary, kind FROM activity_events
        WHERE ts >= ? AND datetime(ts) >= datetime('now', ?) ORDER BY datetime(ts) DESC, id DESC LIMIT 2000""", (pre, window))
    usage = _rows(conn, """SELECT agent, session_id, project, MAX(datetime(ts)) ts,
        SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) tokens, MAX(model) model
        FROM usage_events WHERE ts >= ? AND datetime(ts) >= datetime('now', ?) GROUP BY agent, session_id, project""", (pre, window))
    sessions: dict[tuple, dict[str, Any]] = {}
    for a in acts:  # newest first: the first seen row per session is its last action
        key = (a["agent"], a["session_id"])
        s = sessions.setdefault(key, {"agent": a["agent"], "session_id": a["session_id"], "project": a["project"],
                                      "last_ts": a["ts"], "last_tool": a["name"] if a["kind"] == "tool" else None,
                                      "last_summary": a["summary"] if a["kind"] == "tool" else "", "tools_10m": 0, "tokens_10m": 0, "model": None})
        s["tools_10m"] += a["kind"] == "tool"
        if s["last_tool"] is None and a["kind"] == "tool":
            s["last_tool"], s["last_summary"] = a["name"], a["summary"]
    for u in usage:
        # Subagent tokens are logged under "claude"; their activity under "claude-subagent".
        key = (u["agent"], u["session_id"])
        s = sessions.setdefault(key, {"agent": u["agent"], "session_id": u["session_id"], "project": u["project"], "last_ts": u["ts"],
                                      "last_tool": None, "last_summary": "", "tools_10m": 0, "tokens_10m": 0, "model": None})
        s["tokens_10m"] += int(u["tokens"] or 0)
        s["model"] = u["model"]
        if (parse_ts(u["ts"]) or _now()) > (parse_ts(s["last_ts"]) or _now()):
            s["last_ts"] = u["ts"]
        s["project"] = s["project"] or u["project"]
    for a in _rows(conn, """SELECT s.agent_id,s.project,s.last_activity_at,s.model,
        (SELECT name FROM subagent_tools t WHERE t.agent_id=s.agent_id ORDER BY source_offset DESC LIMIT 1) last_tool,
        (SELECT arg FROM subagent_tools t WHERE t.agent_id=s.agent_id ORDER BY source_offset DESC LIMIT 1) last_summary
        FROM subagents s WHERE s.agent_kind='codex' AND datetime(COALESCE(s.last_activity_at,s.started_at)) >= datetime('now', ?)""", (window,)):
        key = ("codex", a["agent_id"])
        s = sessions.setdefault(key, {"agent": "codex", "session_id": a["agent_id"], "project": a["project"], "last_ts": a["last_activity_at"],
                                      "last_tool": a["last_tool"], "last_summary": a["last_summary"] or "", "tools_10m": 0, "tokens_10m": 0, "model": a["model"]})
        s["last_tool"], s["last_summary"], s["model"] = a["last_tool"] or s["last_tool"], a["last_summary"] or s["last_summary"], a["model"] or s["model"]
    now = _now()
    out = []
    for s in sessions.values():
        last = parse_ts(s["last_ts"])
        s["last_ts"] = _iso(last)
        s["age_s"] = int((now - last).total_seconds()) if last else None
        s["project_name"] = basename(s["project"])
        out.append(s)
    out.sort(key=lambda s: s["age_s"] if s["age_s"] is not None else 1e9)
    return out


# ---------------------------------------------------------------- activity feed

def activity(conn, limit: int = FEED_LEN) -> list[dict[str, Any]]:
    rows = _rows(conn, """SELECT id, agent, session_id, project, ts, kind, name, summary FROM activity_events
        WHERE ts >= ? ORDER BY datetime(ts) DESC, id DESC LIMIT ?""", (_prefilter(7), limit))
    if len(rows) < limit:
        rows = _rows(conn, "SELECT id, agent, session_id, project, ts, kind, name, summary FROM activity_events "
                           "ORDER BY datetime(ts) DESC, id DESC LIMIT ?", (limit,))
    for r in rows:
        r["ts"] = _iso(r["ts"])
        r["project_name"] = basename(r["project"])
    return rows


# ---------------------------------------------------------------- brain graph

def graph(conn, sessions: list[dict[str, Any]], clients: list[dict[str, Any]]) -> dict[str, Any]:
    pre = _prefilter(7)
    last: dict[str, datetime] = {}
    counts: dict[str, dict[str, int]] = {}

    def bump(project, ts, field, n):
        if not project:
            return
        t = parse_ts(ts)
        if t and (project not in last or t > last[project]):
            last[project] = t
        c = counts.setdefault(project, {"activity": 0, "memories": 0})
        c[field] += int(n or 0)

    for r in _rows(conn, "SELECT project, MAX(datetime(ts)) ts, COUNT(*) n FROM activity_events WHERE ts >= ? AND datetime(ts) >= datetime('now','-7 days') GROUP BY project", (pre,)):
        bump(r["project"], r["ts"], "activity", r["n"])
    for r in _rows(conn, "SELECT project, MAX(ts) ts FROM usage_events GROUP BY project"):  # served by usage_project_ts
        bump(r["project"], r["ts"], "activity", 0)
    for r in _rows(conn, "SELECT project, COUNT(*) n FROM usage_events WHERE ts >= ? AND datetime(ts) >= datetime('now','-7 days') GROUP BY project", (pre,)):
        bump(r["project"], None, "activity", r["n"])
    for r in _rows(conn, "SELECT project, MAX(updated_at) ts, COUNT(*) n FROM memories WHERE project IS NOT NULL GROUP BY project"):
        bump(r["project"], r["ts"], "memories", r["n"])
    for s in sessions:
        bump(s["project"], s["last_ts"], "activity", 0)
    ranked = sorted(last, key=lambda p: last[p], reverse=True)[:MAX_PROJECTS]
    chosen = set(ranked)
    mems = _rows(conn, "SELECT id, title, kind, project, created_at FROM memories ORDER BY id DESC LIMIT ?", (MAX_MEMORIES,))
    other = False
    for m in mems:
        m["title"] = (m["title"] or "")[:80]
        if m["project"] is None:
            m["cluster"] = "global"
        elif m["project"] in chosen:
            m["cluster"] = m["project"]
        else:
            m["cluster"], other = "other", True
        m["created_at"] = _iso(m["created_at"])
    memory_ids = {m["id"] for m in mems}
    links = [dict(r) for r in conn.execute("SELECT from_id,to_id,type FROM memory_links ORDER BY type,from_id,to_id").fetchall()
             if r["from_id"] in memory_ids and r["to_id"] in memory_ids]
    projects = [{"id": p, "name": basename(p), "path": p, "last_activity": _iso(last[p]), **counts.get(p, {"activity": 0, "memories": 0})}
                for p in ranked]
    if other:
        projects.append({"id": "other", "name": "other", "path": None, "last_activity": None, "activity": 0,
                         "memories": sum(1 for m in mems if m["cluster"] == "other")})
    agents = []
    for s in sessions[:10]:
        agents.append({"id": f"{s['agent']}:{s['session_id']}", "agent": s["agent"], "session_id": s["session_id"],
                       "project": s["project"] if s["project"] in chosen else None, "project_name": s["project_name"],
                       "active": s["age_s"] is not None and s["age_s"] < 120, "connected": False})
    covered = {(agent_family(a["agent"]), a["project"]) for a in agents}
    for c in clients:
        # A remote client is never one of the local sessions, so it always gets its own node.
        if not c.get("remote") and (c["family"], c["project"]) in covered:
            for a in agents:
                if agent_family(a["agent"]) == c["family"] and a["project"] == c["project"]:
                    a["connected"] = True
            continue
        if not c.get("remote"): covered.add((c["family"], c["project"]))
        agents.append({"id": f"mcp:{c['id']}", "agent": c["family"], "session_id": None,
                       "project": c["project"] if c["project"] in chosen else None, "project_name": c["project_name"],
                       "active": False, "connected": True})
    return {"projects": projects, "memories": mems, "agents": agents, "links": links}


def graph_signature(g: dict[str, Any]) -> str:
    """Changes when nodes or edges change (not on per-project counters), so clients re-layout only when needed."""
    key = [[p["id"] for p in g["projects"]], [(m["id"], m["cluster"], m["kind"]) for m in g["memories"]],
           [(a["id"], a["project"], a["active"], a["connected"]) for a in g["agents"]],
           [(l["from_id"], l["to_id"], l["type"]) for l in g.get("links", [])]]
    return hashlib.sha1(json.dumps(key, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- snapshot and pulses

def memory_counts(conn) -> dict[str, Any]:
    by_kind = {r["kind"]: r["n"] for r in _rows(conn, "SELECT kind, COUNT(*) n FROM memories GROUP BY kind")}
    return {"memories_by_kind": by_kind, "memories_total": sum(by_kind.values())}


def cursor(conn) -> dict[str, int]:
    one = lambda t: conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {t}").fetchone()[0]
    return {"activity": one("activity_events"), "usage": one("usage_events"), "memory": one("memories")}


def build_state(conn) -> dict[str, Any]:
    started = time.monotonic()
    clients = connected_clients(conn)
    sessions = recent_sessions(conn)
    g = graph(conn, sessions, clients)
    state = {
        "now": _iso(_now()), "schema_version": SCHEMA_VERSION,
        "limits": limits(conn), "tokens": tokens(conn),
        "agents": {"connected": clients, "sessions": sessions,
                   "subagents": messages.session_subagents(conn, [s["session_id"] for s in sessions if s["agent"].startswith("claude")])},
        "activity": activity(conn), "graph": g, "graph_sig": graph_signature(g),
        "counts": memory_counts(conn), "cursor": cursor(conn),
    }
    state["build_ms"] = int((time.monotonic() - started) * 1000)
    return state


def pulses_since(conn, since: dict[str, int]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Discrete events newer than the cursor (by id) and no older than 10 minutes, for front-end animation.

    The age filter keeps a first import of historical logs from replaying as a storm of pulses.
    """
    window = f"-{PULSE_MAX_AGE_S} seconds"
    out: list[dict[str, Any]] = []
    new = dict(since)
    acts = _rows(conn, """SELECT id, agent, session_id, project, ts, kind, name, summary FROM activity_events
        WHERE id > ? ORDER BY id LIMIT 500""", (since.get("activity", 0),))
    if acts:
        new["activity"] = acts[-1]["id"]
    now = _now()
    for a in acts:
        t = parse_ts(a["ts"])
        if t and (now - t).total_seconds() <= PULSE_MAX_AGE_S:
            out.append({"type": a["kind"], "agent": a["agent"], "session_id": a["session_id"], "project": a["project"],
                        "project_name": basename(a["project"]), "name": a["name"], "summary": a["summary"], "ts": _iso(t), "id": a["id"]})
    usage = _rows(conn, """SELECT MAX(id) id, agent, session_id, project, MAX(ts) ts, SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) tokens
        FROM usage_events WHERE id > ? AND datetime(ts) >= datetime('now', ?) GROUP BY agent, session_id, project""", (since.get("usage", 0), window))
    new["usage"] = max(new.get("usage", 0), conn.execute("SELECT COALESCE(MAX(id),0) FROM usage_events").fetchone()[0])
    for u in usage:
        out.append({"type": "usage", "agent": u["agent"], "session_id": u["session_id"], "project": u["project"],
                    "project_name": basename(u["project"]), "tokens": int(u["tokens"] or 0), "ts": _iso(u["ts"])})
    mems = _rows(conn, "SELECT id, title, kind, project, created_at, source_agent FROM memories WHERE id > ? ORDER BY id LIMIT 100", (since.get("memory", 0),))
    if mems:
        new["memory"] = mems[-1]["id"]
    for m in mems:
        out.append({"type": "memory", "id": m["id"], "title": (m["title"] or "")[:80], "kind": m["kind"], "project": m["project"],
                    "project_name": basename(m["project"]), "agent": m["source_agent"], "ts": _iso(m["created_at"])})
    return out[-80:], new
