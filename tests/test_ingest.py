from pathlib import Path
from devmem.ingest.claude import parse_line as claude_line
from devmem.ingest.codex import parse_line as codex_line
from devmem.ingest.runner import ingest_file
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
