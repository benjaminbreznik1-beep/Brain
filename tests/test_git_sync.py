import json
import subprocess

from devmem import git_sync
from devmem.db import connection, save_memory


def _run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _configured(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"; _run("git", "init", "--bare", str(remote))
    monkeypatch.setenv("DEVMEM_GIT_REMOTE", str(remote))
    git_sync.write_config({"enabled": True, "repo": "owner/memories", "branch": "main", "author_name": "Test", "author_email": "test@example.invalid", "projects": ["thesis", "devmem", "personal-webpage"], "global_tag": "personal", "interval_s": 300})
    return remote


def _elsewhere(tmp_path, remote, change):
    """Edit the remote the way GitHub would: in another clone, committed and pushed."""
    other = tmp_path / "elsewhere"
    if not other.exists(): _run("git", "clone", "--branch", "main", str(remote), str(other))  # a bare test remote's HEAD still names master
    _run("git", "pull", "--rebase", "origin", "main", cwd=other)
    change(other)
    _run("git", "add", "-A", cwd=other)
    _run("git", "-c", "user.name=Phone", "-c", "user.email=phone@example.invalid", "commit", "-m", "edit elsewhere", cwd=other)
    _run("git", "push", "origin", "HEAD:main", cwd=other)


def _memory(project, tags=""):
    with connection() as conn:
        return save_memory(conn, content="original", title="A memory", kind="note", tags=tags, project=project, source_agent="test")


def test_export_allowlist_and_global_tag(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch)
    thesis = _memory("/work/thesis"); work = _memory("/work/acme"); personal = _memory(None, "personal")
    result = git_sync.sync()
    root = git_sync.repo_path()
    assert not result["errors"]
    assert (root / "memories" / "thesis" / f"{thesis}-a-memory.md").is_file()
    assert (root / "memories" / "global" / f"{personal}-a-memory.md").is_file()
    assert not (root / "memories" / "acme" / f"{work}-a-memory.md").exists()


def test_inbox_import_and_remote_edit_keep_history(tmp_path, monkeypatch):
    remote = _configured(tmp_path, monkeypatch)
    memory_id = _memory("/work/thesis")
    git_sync.sync(); root = git_sync.repo_path()

    def change(other):
        (other / "inbox").mkdir(exist_ok=True)
        (other / "inbox" / "new.md").write_text("---\ntitle: From inbox\nproject: global\ntags: [personal]\n---\nhello\n", encoding="utf-8")
        (other / "memories" / "thesis" / f"{memory_id}-a-memory.md").write_text("---\nid: %s\nkind: note\ntitle: A memory\nproject: thesis\ntags: []\nupdated: 9999-01-01T00:00:00Z\n---\nremote edit\n" % memory_id, encoding="utf-8")
    _elsewhere(tmp_path, remote, change)
    inbox = root / "inbox" / "new.md"
    result = git_sync.sync()
    with connection() as conn:
        row = conn.execute("SELECT content FROM memories WHERE id=?", (memory_id,)).fetchone()
        history = conn.execute("SELECT content FROM memory_revisions WHERE memory_id=?", (memory_id,)).fetchall()
        imported = conn.execute("SELECT content FROM memories WHERE title='From inbox'").fetchone()
    assert not result["errors"]
    assert row["content"] == "remote edit" and history[0]["content"] == "original"
    assert imported["content"] == "hello" and not inbox.exists()


def test_remote_delete_keeps_memory_out_of_sync(tmp_path, monkeypatch):
    remote = _configured(tmp_path, monkeypatch)
    memory_id = _memory("/work/thesis")
    git_sync.sync()
    _elsewhere(tmp_path, remote, lambda other: (other / "memories" / "thesis" / f"{memory_id}-a-memory.md").unlink())
    result = git_sync.sync()
    with connection() as conn:
        assert conn.execute("SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone()
    assert memory_id in result["out_of_sync"]


def test_secret_is_not_exported(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch)
    with connection() as conn:
        memory_id = save_memory(conn, content="token=supersecretvalue", title="secret", project="/work/thesis", source_agent="test")
    result = git_sync.sync()
    assert result["errors"]
    assert not (git_sync.repo_path() / "memories" / "thesis" / f"{memory_id}-secret.md").exists()


def test_first_sync_to_an_empty_remote_creates_the_configured_branch(tmp_path, monkeypatch):
    remote = _configured(tmp_path, monkeypatch)
    _memory("/work/thesis")
    assert not git_sync.sync()["errors"]
    assert "refs/heads/main" in _run("git", "ls-remote", "--heads", str(remote)).stdout


def test_a_second_sync_waits_for_the_lock(tmp_path, monkeypatch):
    import fcntl
    _configured(tmp_path, monkeypatch)
    git_sync.lock_path().parent.mkdir(parents=True, exist_ok=True)
    with git_sync.lock_path().open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert git_sync.sync().get("locked")
    assert not git_sync.sync().get("locked")


def test_both_edited_newer_local_wins_and_remote_version_is_kept_in_history(tmp_path, monkeypatch):
    from devmem.db import update_memory
    remote = _configured(tmp_path, monkeypatch)
    memory_id = _memory("/work/thesis")
    git_sync.sync(); path = git_sync.repo_path() / "memories" / "thesis" / f"{memory_id}-a-memory.md"
    _elsewhere(tmp_path, remote, lambda other: (other / "memories" / "thesis" / f"{memory_id}-a-memory.md").write_text(
        "---\nid: %s\nkind: note\ntitle: A memory\nproject: thesis\ntags: []\nupdated: 2000-01-01T00:00:00Z\n---\nolder remote edit\n" % memory_id, encoding="utf-8"))
    with connection() as conn:
        update_memory(conn, memory_id, content="newer local edit")
    assert not git_sync.sync()["errors"]
    with connection() as conn:
        assert conn.execute("SELECT content FROM memories WHERE id=?", (memory_id,)).fetchone()["content"] == "newer local edit"
        history = [r["content"] for r in conn.execute("SELECT content FROM memory_revisions WHERE memory_id=?", (memory_id,))]
    assert "older remote edit" in history and "newer local edit" in path.read_text(encoding="utf-8")


def test_local_delete_and_leaving_scope_remove_the_file(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch)
    gone = _memory("/work/thesis"); moved = _memory(None, "personal")
    git_sync.sync(); root = git_sync.repo_path()
    with connection() as conn:
        conn.execute("DELETE FROM memories WHERE id=?", (gone,))
        conn.execute("UPDATE memories SET tags='' WHERE id=?", (moved,))
    assert not git_sync.sync()["errors"]
    assert not list((root / "memories").glob(f"**/{gone}-*.md")) and not list((root / "memories").glob(f"**/{moved}-*.md"))


def test_a_failed_run_does_not_poison_the_next_one(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch)
    memory_id = _memory("/work/thesis")
    git_sync.sync(); root = git_sync.repo_path()
    (root / "memories" / "thesis" / "stray.md").write_text("left behind", encoding="utf-8")
    assert not git_sync.sync()["errors"]
    assert not (root / "memories" / "thesis" / "stray.md").exists() and memory_id not in git_sync.status()["out_of_sync"]


def test_deploy_key_only_travels_in_git_ssh_command(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch)
    _memory("/work/thesis"); git_sync.sync()
    monkeypatch.delenv("DEVMEM_GIT_REMOTE")
    env = git_sync._git_env({})
    key = str(git_sync.key_path())
    assert key in env["GIT_SSH_COMMAND"] and "IdentitiesOnly=yes" in env["GIT_SSH_COMMAND"]
    config = _run("git", "config", "--list", cwd=git_sync.repo_path()).stdout
    assert key not in config and "git-sync-key" not in config
    assert git_sync._remote({"repo": "me/memories"}) == "git@github.com:me/memories.git"
