import json
from pathlib import Path
from devmem.ingest.claude import parse_line as claude_line
from devmem.ingest.codex import parse_agent_record, parse_line as codex_line
from devmem.ingest.runner import ingest_file
from devmem.ingest import subagents
from devmem.db import connect

FIXTURES=Path(__file__).parent/'fixtures'

def test_claude_parse_and_dedup(tmp_path):
    lines=(FIXTURES/'claude.jsonl').read_text().splitlines()
    assert claude_line(lines[0])['cache_read_tokens']==2
    log=tmp_path/'x.jsonl';log.write_text('\n'.join(lines)+'\n');conn=connect(tmp_path/'d.db')
    assert ingest_file(conn,log,'claude')==1

def test_codex_token_count_parsing():
    context={};events=[]
    for line in (FIXTURES/'codex.jsonl').read_text().splitlines():
        event,_=codex_line(line,context)
        if event: events.append(event)
    assert events[0]['cache_read_tokens']==8 and events[0]['model']=='gpt-5-codex'

def test_incremental_offset_resume(tmp_path):
    log=tmp_path/'x.jsonl'; log.write_text((FIXTURES/'claude.jsonl').read_text().splitlines()[0]+'\n')
    conn=connect(tmp_path/'d.db'); assert ingest_file(conn,log,'claude')==1
    assert ingest_file(conn,log,'claude')==0


def test_codex_assistant_text_is_not_terminal_until_task_complete():
    context={"session_id": "interactive"}
    text = json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "I am checking."}]}})
    done = json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})
    assert parse_agent_record(text, context)["terminal"] is False
    assert parse_agent_record(done, context)["terminal"] is True


def test_new_codex_user_turn_reopens_a_completed_rollout(tmp_path):
    conn=connect(tmp_path/'d.db')
    base={"session_id": "interactive", "project": str(tmp_path), "ts": "2026-10-29T00:00:00Z", "model": None, "tools": [], "text": None}
    subagents.apply_codex(conn, {**base, "task": "first task", "report_text": None, "finished": False, "terminal": False}, "rollout", 1)
    subagents.apply_codex(conn, {**base, "task": None, "report_text": "first result", "finished": True, "terminal": True}, "rollout", 2)
    conn.execute("UPDATE subagents SET report_msg_id=99,ended='finished' WHERE agent_id='interactive'")
    subagents.apply_codex(conn, {**base, "task": "second task", "report_text": None, "finished": False, "terminal": False}, "rollout", 3)
    assert tuple(conn.execute("SELECT finished,report_msg_id,ended FROM subagents WHERE agent_id='interactive'").fetchone()) == (0,None,None)
