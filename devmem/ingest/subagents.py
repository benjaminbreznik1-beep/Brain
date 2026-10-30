"""Claude Code subagent logs: <session>/subagents/agent-<id>.jsonl plus its agent-<id>.meta.json sidecar.

Kept per agent: task (first user message), one line per tool call, each assistant remark (first 500 chars) and later parent
message, the last assistant text (the report once finished) and whether the newest record ends the run.
Tool results and thinking blocks are never read; every stored text goes through the maintenance secret regex.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..db import now, parse_ts, project_for_cwd
from ..maintenance import SECRET
from ..messages import BODY_MAX, EVENTS_TO, NAME_MAX, STOP_S, SUBJECT_MAX, friendly_name, report_outcome

log = logging.getLogger("devmem.ingest")
_BAD_META: set[tuple[str, float]] = set()

TASK_MAX, COMMAND_MAX, ARG_MAX = 2000, 80, 120
ARG_KEYS = ("file_path", "notebook_path", "command", "pattern", "path", "url", "query", "skill")
HANDBACK = "SubagentHandback"  # Agent SDK subagents deliver their report as this tool call's message
REPORT_WAIT_S = 60  # meta says finished but no end-of-run record yet: wait this long for the last line before posting the report
REMARK_MAX = 500
_HANDOFF = re.compile(r"\b\d{8}-\d{6}-[a-z0-9]+(?:-[a-z0-9]+)*\b")


def agent_id(path: Path) -> str | None:
    name = path.name
    if path.parent.name == "subagents" and name.startswith("agent-") and name.endswith(".jsonl"):
        return name[len("agent-"):-len(".jsonl")]
    return None


_AUTH = re.compile(r"\b(authorization|proxy-authorization)(\s*[:=]\s*)[^'\"\n]+", re.I)
_KEYED = re.compile(r"\b(?:(Bearer|Basic)(\s+)()|(token|x-api-key|api[_-]?key|access[_-]?key|secret|passw(?:or)?d|pwd|cookie)(\s*[:=]\s*)(['\"]?))[^\s'\"&;]+", re.I)
_SHORT_KEY = re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abposr])[-_][\w-]{4,}")


def clean(text: str, size: int) -> str:
    # fix(2026-10-28): commands are stored now, and SECRET only knows long real-looking keys ("Bearer sk-SECRET" slipped through).
    text = SECRET.sub("[redacted]", _SHORT_KEY.sub("[redacted]", _KEYED.sub(lambda m: "".join(g or "" for g in m.groups()) + "[redacted]", _AUTH.sub(r"\1\2[redacted]", text))))  # before truncating, so a cut can never leave half a secret
    return text if len(text) <= size else text[:size - 1] + "…"


def short_arg(tool_input: Any) -> str:
    if not isinstance(tool_input, dict): return ""
    for key in ARG_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return clean(" ".join(value.split()), COMMAND_MAX if key == "command" else ARG_MAX)
    return ""


def _text(content) -> str | None:
    if isinstance(content, str): return content
    if isinstance(content, list):
        parts = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


def _str(value) -> str | None: return value if isinstance(value, str) and value else None


def parse(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("type") not in ("user", "assistant"): return None
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content")
    # fix(2026-10-28): keep only string fields; a dict sessionId or cwd used to abort the whole ingest pass on every retry.
    rec = {"ts": _str(obj.get("timestamp")), "session_id": _str(obj.get("sessionId")), "cwd": _str(obj.get("cwd")), "task": None, "tools": [],
           "text": None, "texts": [], "terminal": False}
    if obj["type"] == "user":
        rec["task"] = _text(content)  # tool_result lists carry no text items, so they are skipped
        rec["terminal"] = obj.get("toolEndsTurn") is True  # the result of SubagentHandback ends the run
        return rec
    handback = False
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict): continue
        if item.get("type") == "tool_use":
            name = str(item.get("name") or "tool")[:80]
            rec["tools"].append((name, short_arg(item.get("input"))))
            message_text = (item.get("input") or {}).get("message") if name == HANDBACK and isinstance(item.get("input"), dict) else None
            if isinstance(message_text, str) and message_text.strip(): rec["text"] = message_text
            handback = handback or name == HANDBACK
        elif item.get("type") == "text" and isinstance(item.get("text"), str) and item["text"].strip():
            rec["text"] = item["text"]
            rec["texts"].append(clean(item["text"].strip(), REMARK_MAX))
    rec["terminal"] = handback or (message.get("stop_reason") in ("end_turn", "stop_sequence") and bool(rec["texts"]) and not rec["tools"])
    return rec


def ensure(conn, aid: str, source_file: str, session_id: str | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO subagents(agent_id,source_file,session_id) VALUES(?,?,?)", (aid, source_file, session_id))


def apply_codex(conn, rec: dict[str, Any], source_file: str, offset: int) -> None:
    aid = rec.get("session_id")
    if not isinstance(aid, str) or not aid: return
    ts = rec.get("ts") if parse_ts(rec.get("ts")) else None
    task = rec.get("task")
    ensure(conn, aid, source_file, aid)
    handoff = _HANDOFF.search(task or "")
    conn.execute("""UPDATE subagents SET agent_kind='codex',agent_type='codex',project=COALESCE(project,?),model=COALESCE(model,?),
        started_at=COALESCE(started_at,?),last_activity_at=CASE WHEN ? IS NOT NULL AND (last_activity_at IS NULL OR datetime(?) > datetime(last_activity_at)) THEN ? ELSE last_activity_at END,
        task=COALESCE(task,?),handoff_id=COALESCE(handoff_id,?),last_text=COALESCE(?,last_text),finished=CASE WHEN ? THEN 0 ELSE MAX(finished,?) END,last_terminal=? WHERE agent_id=?""",
                 (project_for_cwd(rec.get("project")) if rec.get("project") else None, rec.get("model"), ts, ts, ts, ts,
                  clean(task, TASK_MAX) if task else None, handoff.group(0) if handoff else None,
                  clean(rec.get("report_text"), BODY_MAX) if rec.get("report_text") else None, int(bool(task)), int(rec.get("finished", False)), int(rec.get("terminal", False)), aid))
    if task:
        conn.execute("UPDATE subagents SET report_msg_id=NULL,ended=NULL WHERE agent_id=?", (aid,))
        conn.execute("UPDATE messages SET status='open',closed_at=NULL WHERE thread_id=(SELECT thread_id FROM messages WHERE id=(SELECT spawn_msg_id FROM subagents WHERE agent_id=?))", (aid,))
    if rec.get("text"):
        conn.execute("INSERT OR IGNORE INTO subagent_texts(agent_id,ts,role,body,source_offset) VALUES(?,?,?,?,?)",
                     (aid, ts, "agent", rec["text"], offset))
    conn.executemany("INSERT OR IGNORE INTO subagent_tools(agent_id,ts,name,arg,source_offset) VALUES(?,?,?,?,?)",
                     [(aid, ts, name, arg, offset + index) for index, (name, arg) in enumerate(rec.get("tools", []))])


def link_codex_handoffs(conn, claude_root: Path) -> None:
    """Link only task ids visibly created or passed to Bash in exactly one newest Claude session."""
    pending = conn.execute("SELECT agent_id,handoff_id FROM subagents WHERE agent_kind='codex' AND handoff_id IS NOT NULL AND handoff_session_id IS NULL").fetchall()
    for row in pending:
        matches: list[tuple[datetime, str]] = []
        needle = row["handoff_id"]
        for path in (claude_root / "projects").glob("*/*.jsonl") if (claude_root / "projects").exists() else ():
            try:
                for line in path.open(encoding="utf-8", errors="replace"):
                    if needle not in line: continue
                    obj = json.loads(line)
                    message = obj.get("message") if isinstance(obj, dict) and isinstance(obj.get("message"), dict) else {}
                    content = message.get("content")
                    text = content if isinstance(content, str) else json.dumps(content) if isinstance(content, list) else ""
                    bash = '"name": "Bash"' in line or '"name":"Bash"' in line
                    if ("Created task:" in text or bash) and needle in text:
                        sid, at = obj.get("sessionId"), parse_ts(obj.get("timestamp"))
                        if isinstance(sid, str) and at: matches.append((at, sid))
            except (OSError, ValueError): continue
        if matches:
            conn.execute("UPDATE subagents SET handoff_session_id=? WHERE agent_id=?", (max(matches)[1], row["agent_id"]))


def apply(conn, aid: str, rec: dict[str, Any], offset: int) -> None:
    ts = rec["ts"] if parse_ts(rec["ts"]) else None
    texts = [("agent", text) for text in rec["texts"]]
    if rec["task"] and not rec["task"].lstrip().startswith("<"):  # later parent messages (SendMessage); reminders and wrappers start with "<"
        row = conn.execute("SELECT task FROM subagents WHERE agent_id=?", (aid,)).fetchone()
        # Not the task itself: a v10 re-read starts at byte 0 with the task already stored.
        if row and row[0] is not None and row[0] != clean(rec["task"], TASK_MAX): texts.append(("parent", clean(rec["task"].strip(), REMARK_MAX)))
    conn.execute("""UPDATE subagents SET session_id=COALESCE(session_id,?), project=COALESCE(project,?), started_at=COALESCE(started_at,?),
        last_activity_at=CASE WHEN ? IS NOT NULL AND (last_activity_at IS NULL OR datetime(?) > datetime(last_activity_at)) THEN ? ELSE last_activity_at END,
        task=COALESCE(task,?), last_text=COALESCE(?,last_text), last_terminal=? WHERE agent_id=?""",
                 (rec["session_id"], project_for_cwd(rec["cwd"]) if rec["cwd"] else None, ts, ts, ts, ts,
                  clean(rec["task"], TASK_MAX) if rec["task"] else None, clean(rec["text"], BODY_MAX) if rec["text"] else None, int(rec["terminal"]), aid))
    conn.executemany("INSERT OR IGNORE INTO subagent_texts(agent_id,ts,role,body,source_offset) VALUES(?,?,?,?,?)",
                     [(aid, ts, role, body, offset + index) for index, (role, body) in enumerate(texts)])
    # A line is far longer than its number of items, so offset+index never collides with the next line's offset.
    conn.executemany("INSERT OR IGNORE INTO subagent_tools(agent_id,ts,name,arg,source_offset) VALUES(?,?,?,?,?)",
                     [(aid, ts, name, arg, offset + index) for index, (name, arg) in enumerate(rec["tools"])])


def scan_meta(conn, claude_root: Path) -> int:
    """Re-read only sidecars whose mtime changed; returns how many were read."""
    root = claude_root / "projects"
    if not root.exists(): return 0
    known = {row[0]: row[1] for row in conn.execute("SELECT agent_id,meta_mtime FROM subagents")}
    read = 0
    for meta in root.glob("*/*/subagents/agent-*.meta.json"):
        aid = meta.name[len("agent-"):-len(".meta.json")]
        try:
            mtime = meta.stat().st_mtime
            if known.get(aid) == mtime: continue
            data = json.loads(meta.read_text(encoding="utf-8"))
            if not isinstance(data, dict): raise ValueError("not a JSON object")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            if (aid, mtime) not in _BAD_META:  # once per agent and file version; a fixed or rewritten sidecar is read again
                _BAD_META.add((aid, mtime)); log.warning("unreadable subagent sidecar %s: %s", meta, exc)
            continue
        ensure(conn, aid, str(meta.with_name(f"agent-{aid}.jsonl")), meta.parent.parent.name)
        description = data.get("description") if isinstance(data.get("description"), str) else None
        conn.execute("UPDATE subagents SET agent_type=?,description=?,tool_use_id=?,spawn_depth=?,finished=?,meta_mtime=? WHERE agent_id=?",
                     (str(data.get("agentType") or "")[:NAME_MAX] or None, clean(description, SUBJECT_MAX) if description else None,
                      data.get("toolUseId") if isinstance(data.get("toolUseId"), str) else None,
                      data.get("spawnDepth") if isinstance(data.get("spawnDepth"), int) else None, int(data.get("finished") is True), mtime, aid))
        read += 1
    return read



def _label(row) -> str:
    return row["description"] or row["agent_type"] or "subagent"


def _end(row, at: datetime) -> tuple[str, str] | None:
    """(ended, report body) once an agent is done, else None. Done = meta finished, a terminal last record, or no write for STOP_S."""
    last = parse_ts(row["last_activity_at"] or row["started_at"])
    idle = (at - last).total_seconds() if last else float("inf")
    if row["finished"] or row["last_terminal"]:
        if row["tail_partial"] and idle < STOP_S: return None  # the final line is still being written; an earlier text is not the report
        if row["tail_partial"]:
            return "stopped", clean("(no report: agent stopped; its last text follows)\n\n" + row["last_text"], BODY_MAX) if row["last_text"] else "(no report: agent stopped)"
        if not row["last_terminal"] and idle < REPORT_WAIT_S: return None  # meta can land before the last record
        return "finished", row["last_text"] or "(finished without a text report)"
    if idle < STOP_S: return None
    return "stopped", clean("(no report: agent stopped; its last text follows)\n\n" + row["last_text"], BODY_MAX) if row["last_text"] else "(no report: agent stopped)"


def emit_events(conn) -> int:
    """Turn each spawn and each end (report, or stopped) into a feed event (kind=event) in one thread per agent; the end closes the thread."""
    count = 0
    for row in conn.execute("SELECT * FROM subagents WHERE spawn_msg_id IS NULL AND (task IS NOT NULL OR agent_type IS NOT NULL)").fetchall():
        is_codex = row["agent_kind"] == "codex"
        title = row["handoff_id"][16:] if is_codex and row["handoff_id"] else None  # "20261028-135756-codex-chat-demo" -> "codex-chat-demo"
        subject = ("dispatched Codex — " if row["handoff_session_id"] else "started: ") + (title or row["task"] or _label(row)) if is_codex else \
                  "dispatched " + friendly_name(row["agent_type"]) + " — " + _label(row)
        cur = conn.execute("""INSERT INTO messages(project,sender,sender_kind,recipient,kind,subject,body,status,created_at,agent_id)
            VALUES(?,?,'subagent',?,'event',?,?,'open',?,?)""",
                           (row["project"], row["agent_type"] or "subagent", EVENTS_TO,
                            clean(subject, SUBJECT_MAX),
                            row["task"] or row["description"] or "(no task recorded)", row["started_at"] or now(), row["agent_id"]))
        conn.execute("UPDATE messages SET thread_id=id WHERE id=?", (cur.lastrowid,))
        conn.execute("UPDATE subagents SET spawn_msg_id=? WHERE agent_id=?", (cur.lastrowid, row["agent_id"]))
        count += 1
    at = datetime.now(UTC)
    # fix(2026-10-28): most sidecars never say finished, so runs also end on their last record or after STOP_S of silence; this also repairs agents scanned before.
    for row in conn.execute("SELECT * FROM subagents WHERE report_msg_id IS NULL AND spawn_msg_id > 0").fetchall():
        end = _end(row, at)
        if not end: continue
        spawn = conn.execute("SELECT thread_id FROM messages WHERE id=?", (row["spawn_msg_id"],)).fetchone()
        if not spawn:  # the thread was pruned; nothing to attach a report to
            conn.execute("UPDATE subagents SET report_msg_id=0,ended=? WHERE agent_id=?", (end[0], row["agent_id"])); continue
        stamp = row["last_activity_at"] or row["started_at"] or now()
        cur = conn.execute("""INSERT INTO messages(thread_id,reply_to,project,sender,sender_kind,recipient,kind,subject,body,status,created_at,agent_id)
            VALUES(?,?,?,?,'subagent',?,'event',?,?,'open',?,?)""",
                           (spawn["thread_id"], row["spawn_msg_id"], row["project"], row["agent_type"] or "subagent", EVENTS_TO,
                            clean((friendly_name(row["agent_type"]) + ": " + report_outcome(end[1])[0]) if end[0] == "finished" else friendly_name(row["agent_type"]) + ": stopped without a report", SUBJECT_MAX), end[1], stamp, row["agent_id"]))
        conn.execute("UPDATE subagents SET report_msg_id=?,ended=? WHERE agent_id=?", (cur.lastrowid, end[0], row["agent_id"]))
        # An ended agent's thread is done; closed threads age out through message pruning like any other.
        conn.execute("UPDATE messages SET status='closed',closed_at=? WHERE thread_id=? AND status!='closed'", (stamp, spawn["thread_id"]))
        count += 1
    return count
