"""Private Git memory sync.  State is deliberately kept outside SQLite."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .db import connection, now, save_memory, update_memory
from .messages import guard

_KINDS = {"note", "decision", "preference", "snippet", "todo"}


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def config_path() -> Path: return _path("DEVMEM_GIT_SYNC", "~/.devmem/git-sync.json")
def state_path() -> Path: return _path("DEVMEM_GIT_SYNC_STATE", "~/.devmem/git-sync-state.json")
def key_path() -> Path: return _path("DEVMEM_GIT_SYNC_KEY", "~/.devmem/git-sync-key")
def repo_path() -> Path: return _path("DEVMEM_GIT_DIR", "~/.devmem/git-memories")
def lock_path() -> Path: return _path("DEVMEM_GIT_SYNC_LOCK", "~/.devmem/git-sync.lock")


def _read(path: Path, fallback):
    try:
        with path.open(encoding="utf-8") as f: return json.load(f)
    except FileNotFoundError: return fallback
    except (OSError, ValueError) as exc: raise ValueError(f"invalid {path.name}: {exc}") from exc


def config() -> dict:
    data = _read(config_path(), {})
    if not isinstance(data, dict): raise ValueError("git sync config must be an object")
    return data


def _atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":")); f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.chmod(temp, 0o600); os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def write_config(data: dict) -> None: _atomic(config_path(), data)


def _state() -> dict:
    data = _read(state_path(), {"memories": {}, "out_of_sync": [], "last_sync": None, "last_error": None, "pushed": 0, "pulled": 0})
    if not isinstance(data, dict): raise ValueError("git sync state must be an object")
    data.setdefault("memories", {}); data.setdefault("out_of_sync", [])
    return data


def _git_env(cfg: dict) -> dict:
    env = os.environ.copy()
    if not os.environ.get("DEVMEM_GIT_REMOTE"):
        key = key_path()
        known = key.with_name("git-sync-known_hosts")
        env["GIT_SSH_COMMAND"] = f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={known}"
    return env


def _run(cfg: dict, *args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, env=_git_env(cfg), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode: raise RuntimeError((result.stderr or result.stdout or "git failed").strip()[:500])
    return result.stdout.strip()


def _remote(cfg: dict) -> str:
    injected = os.environ.get("DEVMEM_GIT_REMOTE")
    if injected: return injected
    repo = cfg.get("repo", "")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo): raise ValueError("repo must be owner/name")
    return f"ssh://git@ssh.github.com:443/{repo}.git" if cfg.get("ssh_port") == 443 else f"git@github.com:{repo}.git"


def _project(value: str | None) -> str:
    return Path(value).name if value else "global"


def _tags(value: str) -> list[str]: return [x.strip() for x in value.split(",") if x.strip()]
def _scope(row: dict, cfg: dict) -> bool:
    return _project(row.get("project")) in cfg.get("projects", []) or (not row.get("project") and cfg.get("global_tag") in _tags(row.get("tags") or ""))


def in_scope(row: dict) -> bool:
    try: return _scope(row, config())
    except ValueError: return False


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result[:60] or "memory"


def _frontmatter(meta: dict, content: str) -> str:
    tags = ", ".join(meta["tags"])
    review = f"review_after: {meta['review_after']}\n" if meta.get("review_after") else ""
    ident = f"id: {meta['id']}\n" if meta.get("id") is not None else ""
    updated = f"updated: {meta['updated']}\n" if meta.get("updated") else ""
    return f"---\n{ident}kind: {meta['kind']}\ntitle: {meta['title']}\nproject: {meta['project']}\ntags: [{tags}]\n{updated}{review}---\n{content.rstrip()}\n"


def _parse(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"): raise ValueError("frontmatter is required")
    head, sep, content = text[4:].partition("\n---\n")
    if not sep: raise ValueError("frontmatter is not closed")
    data = {}
    for line in head.splitlines():
        key, mark, value = line.partition(":")
        if not mark: raise ValueError("invalid frontmatter")
        value = value.strip(); data[key.strip()] = value
    data["tags"] = [x.strip() for x in data.get("tags", "[]").strip("[]").split(",") if x.strip()]
    if data.get("kind", "note") not in _KINDS: raise ValueError("invalid kind")
    data["kind"] = data.get("kind", "note"); data["project"] = data.get("project", "global")
    return data, content.rstrip("\n")


def _blob(path: Path) -> str:
    data = path.read_bytes(); return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _write_memory(root: Path, row: dict) -> Path:
    folder = _project(row.get("project")); path = root / "memories" / folder / f"{row['id']}-{_slug(row['title'])}.md"
    guard(row["content"], row["title"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_frontmatter({"id": row["id"], "kind": row["kind"], "title": row["title"], "project": folder, "tags": _tags(row["tags"]), "updated": row["updated_at"].replace("+00:00", "Z"), "review_after": row.get("review_after")}, row["content"]), encoding="utf-8")
    return path


def _layout(root: Path) -> None:
    readme = root / "README.md"; agents = root / "AGENTS.md"
    if not readme.exists(): readme.write_text("# devmem memories\n\nPersonal memories synchronized by devmem.\n", encoding="utf-8")
    if not agents.exists(): agents.write_text("# Memory instructions\n\nSearch before editing. Add new memories in `inbox/`; edit existing files in place and bump `updated`. Never put secrets here.\n", encoding="utf-8")
    (root / "memories").mkdir(exist_ok=True); (root / "inbox").mkdir(exist_ok=True)


def _ensure_repo(cfg: dict) -> Path:
    root = repo_path()
    if not (root / ".git").is_dir():
        root.parent.mkdir(parents=True, exist_ok=True); _run(cfg, "clone", _remote(cfg), str(root))
    _run(cfg, "config", "user.name", str(cfg.get("author_name", "devmem")), cwd=root)
    _run(cfg, "config", "user.email", str(cfg.get("author_email", "devmem@localhost")), cwd=root)
    # A run that failed before committing leaves files behind; the DB and the remote are the truth, so drop them.
    try: _run(cfg, "reset", "--hard", cwd=root)
    except RuntimeError: pass  # no commit yet
    _run(cfg, "clean", "-fd", cwd=root)
    _run(cfg, "fetch", "origin", cwd=root)
    branch = cfg.get("branch", "main")
    try: _run(cfg, "rev-parse", "--verify", f"origin/{branch}", cwd=root)
    except RuntimeError:
        _run(cfg, "symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=root)  # an empty remote: first commit goes to the configured branch
        return root
    try: _run(cfg, "rebase", f"origin/{branch}", cwd=root)
    except RuntimeError as exc:
        _run(cfg, "rebase", "--abort", cwd=root)
        raise RuntimeError("git conflict: " + str(exc)) from exc
    return root


def _commit(cfg: dict, root: Path, summary: dict, dry_run: bool) -> bool:
    status = _run(cfg, "status", "--porcelain", cwd=root)
    if not status: return False
    if dry_run: return True
    _run(cfg, "add", "-A", cwd=root)
    _run(cfg, "commit", "-m", f"devmem sync: +{summary['pushed']} ~{summary['pulled']} -{summary['deleted']}", cwd=root)
    try: _run(cfg, "push", "origin", cfg.get("branch", "main"), cwd=root)
    except RuntimeError:
        _run(cfg, "fetch", "origin", cwd=root); _run(cfg, "rebase", f"origin/{cfg.get('branch', 'main')}", cwd=root); _run(cfg, "push", "origin", cfg.get("branch", "main"), cwd=root)
    return True


def sync(*, dry_run: bool = False) -> dict:
    cfg = config(); result = {"pushed": 0, "pulled": 0, "deleted": 0, "errors": [], "out_of_sync": []}
    if not cfg.get("enabled"): return {**result, "disabled": True}
    if dry_run:
        with connection() as conn:
            result["pushed"] = sum(1 for row in conn.execute("SELECT * FROM memories") if _scope(dict(row), cfg))
        result["dry_run"] = True
        return result
    lock_path().parent.mkdir(parents=True, exist_ok=True)
    with lock_path().open("a+") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return {**result, "locked": True}
        state = _state()
        try:
            root = _ensure_repo(cfg); _layout(root)
            files = {str(path.relative_to(root)): path for path in (root / "memories").glob("**/*.md")}
            with connection() as conn:
                rows = {str(row["id"]): dict(row) for row in conn.execute("SELECT * FROM memories").fetchall()}
                known = dict(state["memories"])  # only saved when the whole run succeeds
                for ident, prior in list(known.items()):
                    path = root / prior["path"]
                    if ident not in rows:
                        if path.exists(): path.unlink(); result["deleted"] += 1
                        known.pop(ident, None); continue
                    if not path.exists():
                        result["out_of_sync"].append(int(ident)); continue
                for ident, row in rows.items():
                    if not _scope(row, cfg):
                        prior = known.pop(ident, None)
                        if prior and (root / prior["path"]).exists(): (root / prior["path"]).unlink(); result["deleted"] += 1
                        continue
                    prior = known.get(ident); path = root / prior["path"] if prior else None
                    if prior and ident in result["out_of_sync"]: continue
                    if not prior or not path or not path.exists():
                        try: path = _write_memory(root, row); result["pushed"] += 1
                        except ValueError as exc: result["errors"].append(f"#{ident}: {exc}"); continue
                    else:
                        sha = _blob(path); local_changed = row["updated_at"] != prior.get("db_updated_at"); file_changed = sha != prior.get("blob")
                        if file_changed:
                            try: meta, content = _parse(path); guard(content, meta.get("title"))
                            except (OSError, ValueError) as exc: result["errors"].append(f"{prior['path']}: {exc}"); continue
                            remote_newer = not local_changed or meta.get("updated", "") > row["updated_at"].replace("+00:00", "Z")
                            if remote_newer:
                                update_memory(conn, int(ident), content=content, title=meta.get("title", row["title"]), kind=meta["kind"], tags=", ".join(meta["tags"]), review_after=meta.get("review_after") or "")
                                row = dict(conn.execute("SELECT * FROM memories WHERE id=?", (ident,)).fetchone()); rows[ident] = row; result["pulled"] += 1
                            else:
                                if local_changed:  # both sides edited: the losing remote version goes into history
                                    conn.execute("INSERT INTO memory_revisions(memory_id,project,kind,title,content,tags,pinned,created_at,op,revised_at) VALUES (?,?,?,?,?,?,?,?,'update',?)",
                                                 (int(ident), row["project"], meta["kind"], meta.get("title") or row["title"], content, ", ".join(meta["tags"]), row["pinned"], row["created_at"], now()))
                                try: _write_memory(root, row); result["pushed"] += 1
                                except ValueError as exc: result["errors"].append(f"#{ident}: {exc}"); continue
                        elif local_changed:
                            try: _write_memory(root, row); result["pushed"] += 1
                            except ValueError as exc: result["errors"].append(f"#{ident}: {exc}"); continue
                    known[ident] = {"path": str(path.relative_to(root)), "blob": _blob(path), "db_updated_at": row["updated_at"]}
                for inbox in (root / "inbox").glob("*.md"):
                    try:
                        meta, content = _parse(inbox); guard(content, meta.get("title")); project = meta["project"]
                        tags = meta["tags"] or ([cfg.get("global_tag", "personal")] if project == "global" else [])
                        memory_id = save_memory(conn, content=content, title=meta.get("title") or content[:80], kind=meta["kind"], tags=", ".join(tags), project=None if project == "global" else project, review_after=meta.get("review_after") or "", source_agent="git-sync")
                        row = dict(conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()); path = _write_memory(root, row); inbox.unlink(); known[str(memory_id)] = {"path": str(path.relative_to(root)), "blob": _blob(path), "db_updated_at": row["updated_at"]}; result["pulled"] += 1
                        if not _scope(row, cfg): result["errors"].append(f"{inbox.name}: imported outside allowlist")
                    except (OSError, ValueError) as exc: result["errors"].append(f"{inbox.name}: {exc}")
            if not dry_run: _commit(cfg, root, result, False)
            state.update({"memories": known, "out_of_sync": result["out_of_sync"], "last_sync": now(), "last_error": "; ".join(result["errors"]) or None, "pushed": result["pushed"], "pulled": result["pulled"]})
            if not dry_run: _atomic(state_path(), state)
        except Exception as exc:
            result["errors"].append(str(exc)); state["last_error"] = str(exc); state["last_sync"] = now()
            if not dry_run: _atomic(state_path(), state)
        finally: fcntl.flock(lock, fcntl.LOCK_UN)
    return result


def status() -> dict:
    state = _state(); cfg = config()
    return {"enabled": bool(cfg.get("enabled")), "repo": cfg.get("repo"), "last_sync": state.get("last_sync"), "pushed": state.get("pushed", 0), "pulled": state.get("pulled", 0), "error": state.get("last_error"), "out_of_sync": state.get("out_of_sync", [])}
