from devmem.db import connect, fts_query, project_for_cwd

def test_fts_ranking_and_scope(tmp_path):
    db=tmp_path/'d.db'; conn=connect(db)
    conn.execute("INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at) VALUES('/a','note','other','python', '', 'x','x')")
    conn.execute("INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at) VALUES('/b','decision','Python choice','python python python', '', 'x','x')")
    rows=conn.execute("SELECT m.project FROM memories_fts JOIN memories m ON m.id=memories_fts.rowid WHERE memories_fts MATCH 'python' AND m.project='/b' ORDER BY bm25(memories_fts)").fetchall()
    assert [r[0] for r in rows] == ['/b']

def test_fts_query_accepts_punctuation_and_ranks_multi_term_hits_first(tmp_path):
    conn=connect(tmp_path/'d.db')
    conn.execute("INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at) VALUES(NULL,'note','auth','only auth here', '', 'x','x')")
    conn.execute("INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at) VALUES(NULL,'note','auth bug','auth bug in login', '', 'x','x')")
    for q in ['auth-bug', 'auth "bug', 'auth AND (bug', "auth: bug*"]:
        rows=conn.execute("SELECT m.title FROM memories_fts JOIN memories m ON m.id=memories_fts.rowid WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts)",(fts_query(q),)).fetchall()
        assert [r[0] for r in rows] == ['auth bug','auth']
    assert fts_query('!!! ---') == ''

def test_project_for_cwd_uses_git_root(tmp_path):
    (tmp_path/'repo'/'.git').mkdir(parents=True); (tmp_path/'repo'/'src'/'pkg').mkdir(parents=True)
    assert project_for_cwd(str(tmp_path/'repo'/'src'/'pkg')) == str((tmp_path/'repo').resolve())

def test_iso_timestamps_compare_as_datetimes(tmp_path):
    conn=connect(tmp_path/'d.db')
    # Text comparison would count this (two days old) as within the last day because 'T' > ' '.
    old=conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 day','-1 hour')").fetchone()[0]
    assert conn.execute("SELECT ? >= datetime('now','-1 day')",(old,)).fetchone()[0] == 1
    assert conn.execute("SELECT datetime(?) >= datetime('now','-1 day')",(old,)).fetchone()[0] == 0
