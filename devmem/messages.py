"""Messages between agents and the subagent feed, shared by the MCP tools, the log scanner and the panel."""
from __future__ import annotations

import logging
import re
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .db import now, parse_ts
from .maintenance import SECRET

log = logging.getLogger("devmem.messages")

KINDS = ("message", "question", "finding", "event")
BODY_MAX, SUBJECT_MAX, NAME_MAX = 8000, 200, 100
RATE_PER_MIN, RETENTION_DAYS, STALE_S = 30, 30, 600
STOP_S = 2 * 3600  # no write for this long and not finished: the agent counts as stopped, gets a "(no report)" event and its thread closes
USER = "user"
EVENTS_TO = "#agents"  # Part A events go to a channel, so they only reach inboxes of agents that posted there
_UNREAD_BY_USER = "NOT EXISTS(SELECT 1 FROM message_reads mr WHERE mr.message_id=m.id AND mr.reader='user')"
FRIENDLY_NAMES = {"silent-failure-hunter": "bug hunter", "code-reviewer": "code reviewer", "pr-test-analyzer": "test reviewer",
                  "general-purpose": "worker", "Explore": "explorer", "Plan": "planner"}


def friendly_name(value: str | None) -> str:
    short = (value or "subagent").rsplit(":", 1)[-1]
    return FRIENDLY_NAMES.get(short, short)


def _plain(text: str) -> str:
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)|\[([^]]+)\]\([^)]*\)", lambda m: m.group(1) or m.group(2) or "", text)
    text = re.sub(r"[`*_>#]", "", text)
    return " ".join(text.split())


def _cut(text: str, size: int = 80) -> str:
    text = _plain(text)
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def _sentence(text: str, size: int = 70) -> str:
    plain = _plain(text)
    sentence = re.split(r"(?<=[.!?])\s+", plain, maxsplit=1)[0]
    return _cut(sentence, size)


def report_outcome(text: str) -> tuple[str, bool]:
    """Return a terse deterministic report result and whether it found a problem."""
    plain = _plain(text)
    if re.search(r"\b(no (?:issues|bugs)|nothing found|LGTM|all pass(?:ed)?)\b", plain, re.I): return "no issues found", False
    count = re.search(r"\b(\d+)\s+(findings?|issues?|bugs?|test gaps?)\b", plain, re.I)
    if count:
        amount, noun = int(count.group(1)), count.group(2).lower()
        noun = "test gaps" if "gap" in noun else ("bugs" if "bug" in noun else "issues")
        severity = re.search(r"\(([^)]*(?:critical|high)[^)]*)\)", plain, re.I)
        return f"{amount} {noun} found" + (f" ({_cut(severity.group(1), 38)})" if severity else ""), amount > 0
    passed = re.search(r"\b(\d+)\s+passed\s*/\s*(\d+)\s+failed\b", plain, re.I)
    if passed:
        good, bad = map(int, passed.groups())
        return ("no issues found" if not bad else f"{bad} failed ({good} passed)"), bool(bad)
    severities = re.findall(r"\b(\d+)\s+(critical|high|important|medium|low)\b", plain, re.I)
    if severities:
        amount = sum(int(number) for number, _ in severities)
        detail = ", ".join(f"{number} {level.lower()}" for number, level in severities)
        return f"{amount} issues found ({_cut(detail, 38)})", amount > 0
    headings = re.findall(r"(?im)^\s*(?:#+\s*)?(?:critical|high|important)[^\n]*\n((?:\s*(?:[-*]|\d+[.)])\s+[^\n]+\n?)+)", text)
    found = sum(len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)])\s+", block)) for block in headings)
    if found: return f"{found} issues found", True
    return _sentence(text), False


def headline_for(row: dict[str, Any], agent_type: str | None = None) -> tuple[str, str]:
    """One plain-language feed line and its status icon."""
    kind = row.get("sender_kind")
    who = friendly_name(agent_type or row.get("sender")) if row.get("sender_declared") and kind != "codex" else {"user": "You", "claude": "Claude", "codex": "Codex"}.get(kind, friendly_name(agent_type or row.get("sender")))
    finish = lambda text, icon: (_cut(text, 80), icon)
    if row.get("kind") == "event" and row.get("agent_id"):
        subject = row.get("subject") or ""
        if subject.startswith("started:"):
            return finish(f"{who}: started — {_cut(subject.removeprefix('started:').strip(), 72)}", "▶")
        if subject.startswith("dispatched "):
            return finish(f"Claude: {_cut(subject, 72)}", "▶")
        if subject.startswith("stopped "): return finish(f"{who}: stopped without a report", "■")
        outcome, findings = report_outcome(row.get("body") or "")
        if (row.get("body") or "").startswith("(no report: agent stopped)"): return finish(f"{who}: stopped without a report", "■")
        return finish(f"{who}: {_cut(outcome, max(8, 80 - len(who) - 2))}", "⚠" if findings else "✓")
    subject = row.get("subject") or _sentence(row.get("body") or "", 72)
    icon = "?" if row.get("kind") == "question" else "✉"
    if re.match(re.escape(who) + r"\b", subject, re.I): return finish(subject, icon)  # already names the speaker
    if row.get("kind") == "question": return finish(f"{who} asks: {_cut(subject, max(8, 80 - len(who) - 7))}", "?")
    return finish(f"{who}: {_cut(subject, max(8, 80 - len(who) - 2))}", "✉")


def _rows(conn, sql: str, params=()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def guard(*texts: str | None) -> None:
    if any(text and SECRET.search(text) for text in texts):
        raise ValueError("message rejected: it looks like it contains a secret (key, token or password); nothing was stored")


def _name(value: str | None, field: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > NAME_MAX: raise ValueError(f"{field} must be 1-{NAME_MAX} characters")
    return value


def post(conn, body: str, *, sender: str, sender_kind: str, to: str = "*", subject: str | None = None, kind: str = "message",
         reply_to: int | None = None, project: str | None = None, declared: bool = False) -> int:
    """Store one message and return its id; a reply joins its parent's thread and answers an open question."""
    if kind not in KINDS: raise ValueError("kind must be " + ", ".join(KINDS))
    if not isinstance(body, str) or not body.strip(): raise ValueError("body is required")
    if len(body) > BODY_MAX: raise ValueError(f"body exceeds {BODY_MAX} characters; put long material in a file and post its path")
    if subject is not None and len(subject) > SUBJECT_MAX: raise ValueError(f"subject exceeds {SUBJECT_MAX} characters")
    sender, to = _name(sender, "sender"), _name(to, "to")
    speaker = friendly_name(sender) if declared and sender_kind != "codex" else {"user": "You", "claude": "Claude", "codex": "Codex"}.get(sender_kind, friendly_name(sender))
    derived_subject = subject is None
    if derived_subject: subject = SECRET.sub("[redacted]", _cut(f"{speaker}: {_sentence(body, 72)}", SUBJECT_MAX))
    guard(body, None if derived_subject else subject)  # before anything is written: a rejected secret must never reach the DB
    if conn.execute("SELECT COUNT(*) FROM messages WHERE sender=? AND agent_id IS NULL AND datetime(created_at) >= datetime('now','-60 seconds')",
                    (sender,)).fetchone()[0] >= RATE_PER_MIN:
        raise ValueError(f"rate limit: at most {RATE_PER_MIN} posts per minute per sender")
    parent = None
    if reply_to is not None:
        parent = conn.execute("SELECT * FROM messages WHERE id=?", (reply_to,)).fetchone()
        if not parent: raise ValueError(f"message {reply_to} does not exist")
    cur = conn.execute("""INSERT INTO messages(thread_id,reply_to,project,sender,sender_kind,sender_declared,recipient,kind,subject,body,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,'open',?)""", (parent["thread_id"] if parent else None, reply_to, project if project is not None else (parent["project"] if parent else None),
                                                  sender, sender_kind, int(declared), to, kind, subject, body, now()))
    message_id = cur.lastrowid
    if parent is None: conn.execute("UPDATE messages SET thread_id=id WHERE id=?", (message_id,))
    elif parent["kind"] == "question" and parent["status"] == "open":
        conn.execute("UPDATE messages SET status='answered' WHERE id=?", (reply_to,))
    return message_id


def _with_thread(conn, where: str, params=(), order="m.id", limit: int | None = None) -> list[dict[str, Any]]:
    sql = f"""SELECT m.*, r.status thread_status, r.subject thread_subject FROM messages m LEFT JOIN messages r ON r.id=m.thread_id
        WHERE {where} ORDER BY {order}""" + (" LIMIT ?" if limit else "")
    return _rows(conn, sql, (*params, limit) if limit else params)


def mark_read(conn, ids, reader: str) -> None:
    conn.executemany("INSERT OR IGNORE INTO message_reads(message_id,reader,read_at) VALUES(?,?,?)", [(i, reader, now()) for i in ids])


def inbox(conn, reader: str, unread_only: bool = True, limit: int = 20) -> list[dict[str, Any]]:
    """Messages to the reader, to '*', or to channels the reader posted in; oldest first, then marked read."""
    rows = _with_thread(conn, """m.sender != :me AND (m.recipient=:me OR m.recipient='*' OR (m.recipient LIKE '#%' AND m.recipient IN
        (SELECT recipient FROM messages WHERE sender=:me AND recipient LIKE '#%')))
        AND (:everything OR NOT EXISTS(SELECT 1 FROM message_reads mr WHERE mr.message_id=m.id AND mr.reader=:me))""",
                        {"me": reader, "everything": int(not unread_only)}, order="m.id DESC")[:max(1, min(limit, 100))]
    rows.reverse()
    # fix(2026-10-28): only an unread check consumes messages; re-reading history (unread_only=False) must not mark everything read.
    if unread_only: mark_read(conn, [row["id"] for row in rows], reader)
    return rows


def thread(conn, thread_id: int) -> list[dict[str, Any]]:
    return _with_thread(conn, "m.thread_id=?", (thread_id,))


def last_id(conn, thread_id: int) -> int:
    return conn.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE thread_id=?", (thread_id,)).fetchone()[0]


def newer(conn, thread_id: int, after_id: int) -> list[dict[str, Any]]:
    return _with_thread(conn, "m.thread_id=? AND m.id>?", (thread_id, after_id))


def wait_baseline(conn, thread_id: int, reader: str) -> int:
    """Default after_id for message_wait: the reader's own newest message in the thread, else the thread root.
    So a reply that landed before the first wait (or between two waits) is still returned."""
    row = conn.execute("SELECT MAX(id) FROM messages WHERE thread_id=? AND sender=?", (thread_id, reader)).fetchone()
    return row[0] if row and row[0] else thread_id


def close(conn, thread_id: int) -> int:
    return conn.execute("UPDATE messages SET status='closed',closed_at=? WHERE thread_id=? AND status!='closed'", (now(), thread_id)).rowcount


def reopen(conn, thread_id: int) -> int:
    """A reopened question that already has a reply goes back to answered, everything else to open."""
    return conn.execute("""UPDATE messages SET closed_at=NULL, status=CASE WHEN kind='question' AND EXISTS(SELECT 1 FROM messages c WHERE c.reply_to=messages.id)
        THEN 'answered' ELSE 'open' END WHERE thread_id=? AND status='closed'""", (thread_id,)).rowcount


def prune(conn, days: int = RETENTION_DAYS) -> int:
    """Delete closed threads whose newest message and close time are both older than `days`."""
    window = f"-{int(days)} days"
    deleted = conn.execute("""DELETE FROM messages WHERE thread_id IN (SELECT r.id FROM messages r WHERE r.id=r.thread_id AND r.status='closed'
        AND datetime(COALESCE(r.closed_at,r.created_at)) < datetime('now',?)
        AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.thread_id=r.id AND (m.status!='closed' OR datetime(m.created_at) >= datetime('now',?))))""",
                           (window, window)).rowcount
    _prune_subagents(conn)
    return deleted


def _prune_subagents(conn) -> None:
    """An agent whose spawn thread is gone loses its timeline; its row stays as a tombstone (spawn_msg_id=-1) so the scanner
    never re-posts it, until its log file is gone as well."""
    for (aid,) in conn.execute("""SELECT agent_id FROM subagents s WHERE s.spawn_msg_id > 0
            AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.id=s.spawn_msg_id)""").fetchall():
        conn.execute("DELETE FROM subagent_tools WHERE agent_id=?", (aid,)); conn.execute("DELETE FROM subagent_texts WHERE agent_id=?", (aid,))
        conn.execute("UPDATE subagents SET spawn_msg_id=-1,report_msg_id=COALESCE(report_msg_id,0),task=NULL,last_text=NULL WHERE agent_id=?", (aid,))
    for aid, source in conn.execute("SELECT agent_id,source_file FROM subagents WHERE spawn_msg_id=-1").fetchall():
        if not source or not Path(source).exists():
            conn.execute("DELETE FROM subagent_tools WHERE agent_id=?", (aid,)); conn.execute("DELETE FROM subagents WHERE agent_id=?", (aid,))


def first_line(row: dict[str, Any], size: int = 140) -> str:
    text = row.get("subject") or next((line for line in (row.get("body") or "").splitlines() if line.strip()), "")
    text = " ".join(text.split())
    return text if len(text) <= size else text[:size - 1] + "…"


def feed(conn, limit: int = 20) -> list[dict[str, Any]]:
    rows = _with_thread(conn, "1=1", order="datetime(m.created_at) DESC, m.id DESC", limit=limit)
    read = {row[0] for row in conn.execute(f"SELECT message_id FROM message_reads WHERE reader='user' AND message_id IN ({','.join('?' * len(rows))})",
                                           [row["id"] for row in rows])} if rows else set()
    keys = chat_keys(conn, _roots(conn, [row["thread_id"] for row in rows]))
    out = []
    for row in rows:
        who, text = _preview(conn, row)
        agent = conn.execute("SELECT agent_type,agent_kind,model FROM subagents WHERE agent_id=?", (row["agent_id"],)).fetchone() if row["agent_id"] else None
        headline, icon = headline_for(row, agent_name(dict(agent)) if agent else None)
        out.append({"id": row["id"], "thread_id": row["thread_id"], "sender": row["sender"], "sender_kind": row["sender_kind"],
                    "declared": bool(row["sender_declared"]), "recipient": row["recipient"], "kind": row["kind"], "line": first_line(row),
                    "thread_status": row["thread_status"], "project": row["project"], "agent_id": row["agent_id"], "created_at": row["created_at"],
                    "unread": row["id"] not in read and row["sender_kind"] != "user" and row["kind"] != "event",
                    "chat": keys.get(row["thread_id"]), "who": who, "text": text, "headline": headline, "icon": icon, "mine": row["sender_kind"] == "user",
                    "hue": None if who in ("You", "Claude") else hue(who)})
    for agent in _rows(conn, "SELECT * FROM subagents WHERE spawn_msg_id > 0 AND report_msg_id IS NULL"):
        if subagent_status(agent) != "running": continue
        spawn = conn.execute("SELECT thread_id FROM messages WHERE id=?", (agent["spawn_msg_id"],)).fetchone()
        if not spawn: continue
        tool = conn.execute("SELECT name,arg FROM subagent_tools WHERE agent_id=? ORDER BY source_offset DESC LIMIT 1", (agent["agent_id"],)).fetchone()
        steps = conn.execute("SELECT COUNT(*) FROM subagent_tools WHERE agent_id=?", (agent["agent_id"],)).fetchone()[0]
        detail = (tool["name"] + (" " + tool["arg"] if tool["arg"] else "")) if tool else "working"
        if steps: detail += f" ({steps} steps)"
        who = agent_name(agent)
        out.append({"id": "live:" + agent["agent_id"], "thread_id": spawn["thread_id"], "sender": agent["agent_type"] or "subagent", "sender_kind": "subagent",
                    "declared": False, "recipient": EVENTS_TO, "kind": "event", "line": detail, "thread_status": "open", "project": agent["project"],
                    "agent_id": agent["agent_id"], "created_at": agent["last_activity_at"] or agent["started_at"], "unread": False,
                    "chat": keys.get(spawn["thread_id"]), "who": who, "text": detail, "headline": _cut(f"{who}: working — {detail}"), "icon": "▶", "mine": False,
                    "hue": hue(who)})
    out.sort(key=lambda item: parse_ts(item["created_at"]) or _EPOCH, reverse=True)
    return out[:limit]


def unread_count(conn) -> int:
    """Unread messages for the user; Part A events are traffic, not mail, so they never count."""
    return conn.execute(f"SELECT COUNT(*) FROM messages m WHERE m.kind!='event' AND m.sender_kind!='user' AND {_UNREAD_BY_USER}").fetchone()[0]


def badge(conn) -> int:
    """Threads needing the user: an unread open question, or an open thread addressed to 'user' (each thread counted once)."""
    try:
        return conn.execute(f"""SELECT COUNT(DISTINCT m.thread_id) FROM messages m JOIN messages r ON r.id=m.thread_id
            WHERE (m.kind='question' AND m.status='open' AND r.status!='closed' AND m.sender_kind!='user' AND {_UNREAD_BY_USER})
               OR (m.id=r.id AND r.recipient='user' AND r.status='open')""").fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc): log.exception("messages badge failed")
        return 0  # a pre-v9 database (read-only connection) has no message tables


def snapshot(conn, limit: int = 20) -> dict[str, Any]:
    return {"items": feed(conn, limit), "unread": unread_count(conn), "badge": badge(conn), "sig": signature(conn)}


def threads(conn, project: str = "", agent: str = "", kind: str = "", status: str = "", question: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    where, params = ["r.id=r.thread_id"], []
    if project: where.append("r.project=?"); params.append(project)
    if status: where.append("r.status=?"); params.append(status)
    if agent: where.append("EXISTS(SELECT 1 FROM messages a WHERE a.thread_id=r.id AND (a.sender=? OR a.recipient=?))"); params += [agent, agent]
    if kind: where.append("EXISTS(SELECT 1 FROM messages k WHERE k.thread_id=r.id AND k.kind=?)"); params.append(kind)
    if question: where.append("EXISTS(SELECT 1 FROM messages q WHERE q.thread_id=r.id AND q.kind='question')")
    rows = _rows(conn, f"""SELECT r.*, COUNT(m.id) messages, MAX(m.created_at) last_at, SUM(m.kind='question') questions,
        SUM(m.kind!='event' AND m.sender_kind!='user' AND {_UNREAD_BY_USER}) unread
        FROM messages r JOIN messages m ON m.thread_id=r.id WHERE {' AND '.join(where)}
        GROUP BY r.id ORDER BY datetime(MAX(m.created_at)) DESC, r.id DESC LIMIT ?""", (*params, limit))
    for row in rows: row["line"] = first_line(row)
    return rows


def facets(conn) -> dict[str, list[str]]:
    return {"projects": [r[0] for r in conn.execute("SELECT DISTINCT project FROM messages WHERE project IS NOT NULL ORDER BY project")],
            "agents": [r[0] for r in conn.execute("SELECT sender FROM messages UNION SELECT recipient FROM messages WHERE recipient NOT IN ('*') ORDER BY 1")]}


# ---------------------------------------------------------------- subagents (Part A)

def subagent_status(row: dict[str, Any]) -> str:
    """finished (meta or a terminal last record), stopped (silent for STOP_S), stale (silent for STALE_S) or running."""
    if row.get("finished") or row.get("last_terminal"): return "finished"
    last = parse_ts(row.get("last_activity_at") or row.get("started_at"))
    idle = (datetime.now(UTC) - last).total_seconds() if last else float("inf")
    return "stopped" if idle > STOP_S else "stale" if idle > STALE_S else "running"


def subagent(conn, agent_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM subagents WHERE agent_id=?", (agent_id,)).fetchone()
    if not row: return None
    result = dict(row)
    result["status"] = subagent_status(result)
    result["report"] = result["last_text"] if result["status"] == "finished" else None
    spawn = conn.execute("SELECT thread_id FROM messages WHERE id=?", (result["spawn_msg_id"],)).fetchone() if (result["spawn_msg_id"] or 0) > 0 else None
    result["thread_id"] = spawn[0] if spawn else None  # None once the thread was pruned: no link to a missing page
    result["tools"] = _rows(conn, "SELECT ts,name,arg FROM subagent_tools WHERE agent_id=? ORDER BY source_offset", (agent_id,))
    result["texts"] = _rows(conn, "SELECT ts,role,body FROM subagent_texts WHERE agent_id=? ORDER BY source_offset", (agent_id,))
    result["tokens"] = conn.execute("SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens),0) FROM usage_events WHERE source_file=?",
                                    (result["source_file"],)).fetchone()[0] if result["source_file"] else 0
    return result


def session_subagents(conn, session_ids, max_age_s: int = 3600, per_session: int = 6) -> dict[str, list[dict[str, Any]]]:
    """Recent subagents per parent session, each with its latest tool call, for the Agents panel."""
    ids = [s for s in dict.fromkeys(session_ids) if s]
    if not ids: return {}
    rows = _rows(conn, f"""SELECT s.agent_id,s.session_id,s.handoff_session_id,s.agent_type,s.agent_kind,s.model,s.description,s.started_at,s.last_activity_at,s.finished,s.last_terminal,
        (SELECT name FROM subagent_tools t WHERE t.agent_id=s.agent_id ORDER BY source_offset DESC LIMIT 1) last_tool,
        (SELECT arg FROM subagent_tools t WHERE t.agent_id=s.agent_id ORDER BY source_offset DESC LIMIT 1) last_arg
        FROM subagents s WHERE (s.session_id IN ({','.join('?' * len(ids))}) OR s.handoff_session_id IN ({','.join('?' * len(ids))}))
        AND datetime(COALESCE(s.last_activity_at,s.started_at)) >= datetime('now',?)
        ORDER BY datetime(COALESCE(s.last_activity_at,s.started_at)) DESC""", (*ids, *ids, f"-{max_age_s} seconds"))
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row["status"] = subagent_status(row)
        row["agent_type"] = agent_name(row)
        group = out.setdefault(row["handoff_session_id"] or row["session_id"], [])
        if len(group) < per_session: group.append(row)
    return out


# ---------------------------------------------------------------- chat view

ATTACH_BEFORE, ATTACH_AFTER = timedelta(minutes=5), timedelta(minutes=15)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def hue(name: str) -> int:
    """Stable color per member name, shared by the chat page and the Brain panel."""
    return zlib.crc32(name.encode()) % 360


def short_type(agent_type: str | None) -> str:
    return (agent_type or "subagent").rsplit(":", 1)[-1]


def agent_name(row: dict[str, Any]) -> str:
    if row.get("agent_kind") == "codex":
        model = row.get("model") or ""
        return "Codex" + (f" ({model})" if model else "")
    return friendly_name(row.get("agent_type"))


def _in(column: str, values: list) -> tuple[str, list]:
    return f"{column} IN ({','.join('?' * len(values))})", list(values)


def chat_keys(conn, roots: list[dict[str, Any]]) -> dict[int, str]:
    """Chat for each thread root: s:<session> for a subagent's thread, or for a message_post thread started in the project of exactly one
    Claude session active at that time; c:#channel for other channel threads; t:<thread id> for the rest."""
    keys: dict[int, str] = {}
    agents = [r["agent_id"] for r in roots if r.get("agent_id")]
    sessions: dict[str, tuple[str | None, str | None, str]] = {}
    for start in range(0, len(agents), 500):
        where, params = _in("agent_id", agents[start:start + 500])
        sessions.update({row[0]: (row[1], row[2], row[3]) for row in conn.execute(
            f"SELECT agent_id,session_id,handoff_session_id,agent_kind FROM subagents WHERE {where}", params)})
    pending: dict[str, list[dict[str, Any]]] = {}
    for r in roots:
        agent = sessions.get(r.get("agent_id"))
        if agent and agent[1]: keys[r["id"]] = "s:" + agent[1]
        elif agent and agent[2] == "codex": keys[r["id"]] = "d:" + r["agent_id"]
        elif agent and agent[0]: keys[r["id"]] = "s:" + agent[0]
        elif r.get("project") and not r.get("agent_id"): pending.setdefault(r["project"], []).append(r)
    for project, rows in pending.items():
        times = [t for t in (parse_ts(r["created_at"]) for r in rows) if t]
        if not times: continue
        since = (min(times) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        windows = [(sid, parse_ts(a), parse_ts(b)) for sid, a, b in conn.execute(
            """SELECT session_id,MIN(ts),MAX(ts) FROM usage_events WHERE agent='claude' AND project=? AND ts >= ? AND session_id IS NOT NULL
               AND NOT EXISTS(SELECT 1 FROM ingest_offsets io WHERE io.source_file=usage_events.source_file AND io.context LIKE '%devmem_spawned%') GROUP BY session_id""", (project, since))]
        for r in rows:
            if r.get("sender_kind") == "codex": continue
            at = parse_ts(r["created_at"])
            match = [sid for sid, a, b in windows if at and a and b and a - ATTACH_BEFORE <= at <= b + ATTACH_AFTER]
            if len(match) == 1: keys[r["id"]] = "s:" + match[0]
        codex_runs = [(row[0], parse_ts(row[1]), parse_ts(row[2]), row[3] or "", row[4]) for row in conn.execute(
            "SELECT agent_id,started_at,last_activity_at,handoff_id,handoff_session_id FROM subagents WHERE agent_kind='codex' AND project=?", (project,))]
        for r in rows:
            if r.get("sender_kind") != "codex": continue
            at, sender = parse_ts(r["created_at"]), r.get("sender") or ""
            # fix(2026-10-28): two runs within the window left Codex's own posts in separate chats; the handoff template signs posts with the task id
            # (prefix match: ids stored before the full-slug fix were cut at the first hyphen)
            match = [run for run in codex_runs if run[3] and (sender == run[3] or sender.startswith(run[3] + "-"))]
            if len(match) != 1: match = [run for run in codex_runs if at and run[1] and run[2] and run[1] - ATTACH_BEFORE <= at <= run[2] + ATTACH_AFTER]
            if len(match) == 1: keys[r["id"]] = ("s:" + match[0][4]) if match[0][4] else "d:" + match[0][0]
    for r in roots:
        if r["id"] not in keys:
            channel = r.get("recipient") or ""
            keys[r["id"]] = "c:" + channel if channel.startswith("#") and channel != EVENTS_TO else f"t:{r['id']}"
    return keys


def _roots(conn, thread_ids=None) -> list[dict[str, Any]]:
    if thread_ids is None: return _rows(conn, "SELECT id,agent_id,project,recipient,sender_kind,created_at,subject,body FROM messages WHERE id=thread_id")
    ids = list(dict.fromkeys(thread_ids))
    if not ids: return []
    where, params = _in("id", ids)
    return _rows(conn, f"SELECT id,agent_id,project,recipient,sender_kind,created_at,subject,body FROM messages WHERE id=thread_id AND {where}", params)


def _session_agents(conn, session_id: str) -> list[dict[str, Any]]:
    rows = _rows(conn, """SELECT * FROM subagents WHERE (session_id=? OR handoff_session_id=?) AND spawn_msg_id > 0
        ORDER BY datetime(COALESCE(started_at,last_activity_at)), agent_id""", (session_id, session_id))
    seen: dict[str, int] = {}
    for row in rows:
        base = agent_name(row); seen[base] = seen.get(base, 0) + 1
        row["name"] = base if seen[base] == 1 else f"{base} {seen[base]}"  # two reviewers of one type stay apart
        row["status"] = subagent_status(row)
    return rows


def _later(a, b) -> bool:
    return (parse_ts(a) or _EPOCH) > (parse_ts(b) or _EPOCH)


def _member_of(m: dict[str, Any], by_type: dict[str, dict], session: bool) -> str:
    if m["sender_kind"] == "user": return USER
    if session and m["sender_kind"] == "claude" and not m["sender_declared"]: return "parent"
    agent = by_type.get(m["sender"]) or by_type.get(short_type(m["sender"]))
    return "agent:" + agent["agent_id"] if agent and m["sender_kind"] == "subagent" else "x:" + m["sender"]


def chat(conn, key: str) -> dict[str, Any] | None:
    """Every bubble of one chat, oldest first: tasks, remarks, tool-call groups, reports, posted messages and user replies."""
    kind, _, ident = key.partition(":")
    if kind not in ("s", "t", "c", "d") or not ident: return None
    roots = _roots(conn, [int(ident)] if kind == "t" and ident.isdigit() else None)
    keys = chat_keys(conn, roots)
    thread_ids = [r["id"] for r in roots if keys.get(r["id"]) == key]
    if kind == "s": agents = _session_agents(conn, ident)
    elif kind == "d":
        agent = conn.execute("SELECT * FROM subagents WHERE agent_id=? AND agent_kind='codex' AND spawn_msg_id > 0", (ident,)).fetchone()
        agents = [dict(agent)] if agent else []
        for a in agents: a["name"], a["status"] = agent_name(a), subagent_status(a)
    else: agents = []
    if not thread_ids and not agents: return None
    where, params = _in("m.thread_id", thread_ids) if thread_ids else ("0", [])
    items_m = _with_thread(conn, where, params)
    by_id = {a["agent_id"]: a for a in agents}
    by_type: dict[str, dict] = {}
    for a in agents: by_type.setdefault(a["agent_type"] or "subagent", a); by_type.setdefault(short_type(a["agent_type"]), a)
    members: dict[str, dict[str, Any]] = {USER: {"name": "You", "kind": "user", "hue": None}}
    if kind == "s": members["parent"] = {"name": "Claude", "kind": "claude", "hue": None}
    for a in agents:
        members["agent:" + a["agent_id"]] = {"name": a["name"], "kind": "subagent", "hue": hue(a["name"]), "agent_id": a["agent_id"], "status": a["status"],
                                             "description": a["description"], "type": a["agent_type"]}
    items: list[tuple[tuple, dict[str, Any]]] = []

    def add(phase: int, item: dict[str, Any]) -> None:
        items.append(((parse_ts(item["at"]) or _EPOCH, phase, len(items)), item))

    reports: dict[str, str] = {}
    for m in items_m:
        agent = by_id.get(m["agent_id"]) if m["kind"] == "event" else None
        headline, icon = headline_for(m, agent_name(agent) if agent else None)
        base = {"key": f"m:{m['id']}", "type": "bubble", "at": m["created_at"], "message_id": m["id"], "thread_id": m["thread_id"], "agent_id": m["agent_id"],
                "headline": headline, "icon": icon}
        if agent and m["id"] == agent["spawn_msg_id"]:
            if agent.get("agent_kind") == "codex" and not agent.get("handoff_session_id"):
                add(0, {**base, "who": "agent:" + agent["agent_id"], "text": m["body"], "label": "task", "agent_id": agent["agent_id"]})
            else:
                add(0, {**base, "who": "parent", "to": "@" + agent["name"], "text": m["body"], "label": "task", "agent_id": agent["agent_id"]})
        elif agent and m["id"] == agent["report_msg_id"]:
            reports[agent["agent_id"]] = m["body"]
            add(2, {**base, "who": "agent:" + agent["agent_id"], "text": m["body"], "label": "report" if agent.get("ended") != "stopped" else "stopped"})
        else:
            who = _member_of(m, by_type, kind == "s")
            if who.startswith("x:") and who not in members:
                members[who] = {"name": m["sender"], "kind": m["sender_kind"], "hue": hue(m["sender"]), "declared": bool(m["sender_declared"])}
            to = m["recipient"] if m["recipient"] not in ("*", EVENTS_TO) else None
            add(1, {**base, "who": who, "to": to, "text": m["body"], "subject": m["subject"] if m["id"] == m["thread_id"] else None,
                    "label": m["kind"] if m["kind"] in ("question", "finding") else None, "status": m["status"] if m["kind"] == "question" else None})
    for a in agents:
        aid, member = a["agent_id"], "agent:" + a["agent_id"]
        timeline = [(r["source_offset"], 0, r) for r in _rows(conn, "SELECT id,ts,role,body,source_offset FROM subagent_texts WHERE agent_id=?", (aid,))]
        timeline += [(r["source_offset"], 1, r) for r in _rows(conn, "SELECT id,ts,name,arg,source_offset FROM subagent_tools WHERE agent_id=?", (aid,))]
        timeline.sort(key=lambda entry: (entry[0], entry[1]))
        report, group = (reports.get(aid) or "").strip(), None
        for _, is_tool, r in timeline:
            if is_tool:
                if group is None:
                    group = {"key": f"g:{aid}:{r['id']}", "type": "tools", "who": member, "agent_id": aid, "at": r["ts"], "tools": []}
                    add(1, group)
                group["tools"].append({"name": r["name"], "arg": r["arg"]})
                if r["ts"]: group["at"] = r["ts"]
                continue
            group = None
            body = r["body"].rstrip("…")
            if r["role"] == "agent" and report and report.startswith(body): continue  # the closing text is already the report bubble
            add(1, {"key": f"x:{r['id']}", "type": "bubble", "at": r["ts"], "agent_id": aid, "text": r["body"],
                    **({"who": "parent", "to": "@" + a["name"]} if r["role"] == "parent" else {"who": member})})
    # A tool group sorts by its last call, so a growing group moves down like a typing indicator; re-key after the groups filled in.
    items = [((parse_ts(item["at"]) or _EPOCH, order[1], order[2]), item) for order, item in items]
    items.sort(key=lambda pair: pair[0])
    out = [item for _, item in items]
    for item in out:
        item["mine"] = item["who"] == USER
        if item["type"] == "tools": item["count"] = len(item["tools"])
    headlines = [item for item in out if item.get("message_id")]
    for a in agents:
        if a["status"] != "running": continue
        tool_rows = _rows(conn, "SELECT name,arg FROM subagent_tools WHERE agent_id=? ORDER BY source_offset", (a["agent_id"],))
        last = tool_rows[-1] if tool_rows else None
        tool = (last["name"] + (" " + last["arg"] if last["arg"] else "")) if last else "working"
        if tool_rows: tool += f" ({len(tool_rows)} steps)"
        name = agent_name(a)
        headlines.append({"key": "live:" + a["agent_id"], "type": "live", "at": a["last_activity_at"] or a["started_at"], "who": "agent:" + a["agent_id"],
                          "agent_id": a["agent_id"], "icon": "▶", "headline": _cut(f"{name}: working — {tool}"),
                          "text": "\n".join(t["name"] + (" " + t["arg"] if t["arg"] else "") for t in tool_rows)})
    headlines.sort(key=lambda item: parse_ts(item["at"]) or _EPOCH)
    title, project = _chat_title(conn, key, roots, thread_ids, agents)
    statuses = {m["thread_status"] for m in items_m if m["id"] == m["thread_id"]}
    return {"key": key, "kind": kind, "title": title, "project": project, "members": members, "items": out, "headlines": headlines, "thread_ids": thread_ids,
            "status": "open" if "open" in statuses else (statuses.pop() if len(statuses) == 1 else "mixed" if statuses else None),
            "target": _target_from(out, items_m, by_id, members)}


def _chat_title(conn, key: str, roots, thread_ids, agents) -> tuple[str, str | None]:
    kind, _, ident = key.partition(":")
    mine = [r for r in roots if r["id"] in thread_ids]
    project = next((r["project"] for r in mine if r["project"]), None) or next((a["project"] for a in agents if a["project"]), None)
    if kind == "s":
        if not project:
            row = conn.execute("SELECT project FROM usage_events WHERE session_id=? AND project IS NOT NULL LIMIT 1", (ident,)).fetchone()
            project = row[0] if row else None
        return (Path(project).name if project else "Session") + " · " + ident[:8], project
    if kind == "d":
        agent = next(iter(agents), None) or conn.execute("SELECT * FROM subagents WHERE agent_id=?", (ident,)).fetchone()
        agent = dict(agent) if agent else {}
        project = agent.get("project") or project
        return "Codex · " + (Path(project).name if project else "Project") + " · " + first_line({"body": agent.get("task") or ""}, 80), project
    if kind == "c": return ident, project
    root = mine[0] if mine else {}
    return first_line(root, 80) if root else f"Thread {ident}", project


def _target_from(items, items_m, by_id, members) -> dict[str, Any] | None:
    """Who a user message goes to: the newest open question from an agent, else the last agent that spoke."""
    question = next((m for m in reversed(items_m) if m["kind"] == "question" and m["status"] == "open" and m["sender_kind"] != "user"), None)
    if question: return {"reply_to": question["id"], "to": question["sender"], "name": question["sender"], "why": "open question"}
    for item in reversed(items):
        if item["type"] != "bubble" or item["who"] == USER: continue
        agent = by_id.get(item.get("agent_id"))
        if item["who"].startswith("agent:") and agent:
            reply = item.get("message_id") or agent["report_msg_id"] or agent["spawn_msg_id"]
            if reply and reply > 0: return {"reply_to": reply, "to": agent["agent_type"] or "subagent", "name": agent["name"], "why": "last to speak"}
        elif item.get("message_id"):
            m = next(x for x in items_m if x["id"] == item["message_id"])
            if m["sender_kind"] == "system": continue
            if item["who"] == "parent" and agent: continue  # a task bubble: the parent has no inbox name here; keep looking
            return {"reply_to": m["id"], "to": m["sender"], "name": members.get(item["who"], {}).get("name", m["sender"]), "why": "last to speak"}
    agent_messages = [m for m in items_m if m["sender_kind"] != "system"]
    if agent_messages:
        last = agent_messages[-1]
        return {"reply_to": last["id"], "to": last["sender"] if last["sender_kind"] != "user" else "*", "name": last["sender"], "why": "newest message"}
    return None


def chats(conn, project: str = "", agent: str = "", kind: str = "", status: str = "", question: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    """The chat list: one row per chat, newest first, with a preview of its last bubble and its unread count."""
    rows = threads(conn, project, agent, kind, status, question, limit=5000)
    keys = chat_keys(conn, rows)
    grouped: dict[str, dict[str, Any]] = {}
    for t in rows:
        c = grouped.setdefault(keys[t["id"]], {"key": keys[t["id"]], "kind": keys[t["id"]][0], "threads": [], "last_at": t["last_at"], "unread": 0,
                                               "questions": 0, "open": False})
        c["threads"].append(t["id"]); c["unread"] += t["unread"] or 0; c["questions"] += t["questions"] or 0; c["open"] |= t["status"] == "open"
        if _later(t["last_at"], c["last_at"]): c["last_at"] = t["last_at"]
    out = []
    for c in list(grouped.values()):
        where, params = _in("thread_id", c["threads"])
        last = conn.execute(f"SELECT * FROM messages WHERE {where} ORDER BY datetime(created_at) DESC, id DESC LIMIT 1", params).fetchone()
        c["who"], c["preview"] = _preview(conn, dict(last)) if last else ("", "")
        agent = conn.execute("SELECT agent_type,agent_kind,model FROM subagents WHERE agent_id=?", (last["agent_id"],)).fetchone() if last and last["agent_id"] else None
        c["headline"] = headline_for(dict(last), agent_name(dict(agent)) if agent else None)[0] if last else ""
        ident = c["key"][2:]
        agent_rows = []
        if c["kind"] == "s":
            row = conn.execute("""SELECT t.ts,t.body,t.role,s.agent_type FROM subagent_texts t JOIN subagents s ON s.agent_id=t.agent_id
                WHERE s.session_id=? ORDER BY datetime(t.ts) DESC, t.id DESC LIMIT 1""", (ident,)).fetchone()
            if row and _later(row["ts"], c["last_at"]):
                c["last_at"], c["who"], c["preview"] = row["ts"], ("Claude" if row["role"] == "parent" else short_type(row["agent_type"])), first_line({"body": row["body"]}, 90)
            active = conn.execute("SELECT MAX(last_activity_at) FROM subagents WHERE session_id=?", (ident,)).fetchone()[0]
            if active and _later(active, c["last_at"]): c["last_at"] = active
            c["running"] = conn.execute("SELECT COUNT(*) FROM subagents WHERE session_id=? AND report_msg_id IS NULL AND spawn_msg_id > 0", (ident,)).fetchone()[0]
            agent_rows = _rows(conn, "SELECT * FROM subagents WHERE session_id=? AND spawn_msg_id > 0", (ident,))
        elif c["threads"]:
            where_agents, params_agents = _in("m.thread_id", c["threads"])
            agent_rows = _rows(conn, f"SELECT DISTINCT s.* FROM subagents s JOIN messages m ON m.agent_id=s.agent_id WHERE {where_agents}", params_agents)
        unanswered = conn.execute(f"SELECT COUNT(*) FROM messages m WHERE {where} AND m.kind='question' AND m.status='open' AND m.recipient='user'", params).fetchone()[0]
        if agent_rows:
            states = [subagent_status(row) for row in agent_rows]
            done, working = sum(state == "finished" for state in states), sum(state == "running" for state in states)
            c["running"] = working
            parts = [f"{len(states)} agent" + ("s" if len(states) != 1 else "")]
            if done: parts.append(f"{done} done")
            if working: parts.append(f"{working} working")
            if unanswered: parts.append(f"{unanswered} question for you")
            c["state"] = " · ".join(parts)
        else: c["state"] = "1 thread" + (f" · {unanswered} question for you" if unanswered else "")
        c["title"], c["project"] = _chat_title(conn, c["key"], _roots(conn, c["threads"]), c["threads"], [])
        c["hue"] = hue(c["title"])
        out.append(c)
    out.sort(key=lambda c: parse_ts(c["last_at"]) or _EPOCH, reverse=True)
    return out[:limit]


def _preview(conn, m: dict[str, Any]) -> tuple[str, str]:
    if m["kind"] == "event" and m["agent_id"]:
        agent = conn.execute("SELECT agent_type,spawn_msg_id FROM subagents WHERE agent_id=?", (m["agent_id"],)).fetchone()
        if agent and agent["spawn_msg_id"] == m["id"]: return "Claude", "@" + short_type(agent["agent_type"]) + " " + first_line({"body": m["body"]}, 90)
        return short_type(m["sender"]), first_line({"body": m["body"]}, 90)
    return ("You" if m["sender_kind"] == "user" else m["sender"]), first_line({"body": m["body"]}, 90)


def chat_post(conn, key: str, body: str) -> int:
    """Post the user's message into a chat: a reply to the chat's target, so an agent waiting in message_wait gets it."""
    current = chat(conn, key)
    if not current: raise LookupError("chat not found")
    target = current["target"]
    if not target: raise LookupError("this chat has no message to reply to")
    message_id = post(conn, body, sender=USER, sender_kind="user", to=target["to"] if target["to"] != USER else "*", reply_to=target["reply_to"])
    mark_read(conn, [message_id], USER)
    from .dispatch import enqueue
    enqueue(conn, message_id, key)
    return message_id


def mark_chat_read(conn, current: dict[str, Any]) -> None:
    ids = [item["message_id"] for item in current["items"] if item.get("message_id")]
    mark_read(conn, ids, USER)


def signature(conn) -> list[int]:
    """Changes whenever a bubble could appear or change: new or re-statused messages, new tool calls or remarks, agents ending."""
    return list(conn.execute("""SELECT (SELECT COALESCE(MAX(id),0) FROM messages), (SELECT COUNT(*) FROM messages WHERE status!='open'),
        (SELECT COALESCE(MAX(id),0) FROM subagent_tools), (SELECT COALESCE(MAX(id),0) FROM subagent_texts),
        (SELECT COUNT(*) FROM subagents WHERE report_msg_id IS NOT NULL)""").fetchone())
