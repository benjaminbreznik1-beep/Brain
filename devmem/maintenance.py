"""Automatic memory maintenance: mechanical evidence, guarded LLM proposals, one short apply transaction."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .db import audit_memories, connection, maintenance_failing_since, now, parse_ts, restore_memory, validate_content, validate_review_after

KINDS = {"note", "decision", "preference", "snippet", "todo"}
ACTIONS = {"update", "merge", "supersede", "keep", "flag"}
MODEL_ACTIONS = ACTIONS | {"none"}  # "none" is never stored as an action: it becomes a closed no_change row
TRIGGERS = {"startup", "session_end", "manual"}
# The user's own rules: flag-only whatever maintain.json says (config may add kinds, never remove these).
ALWAYS_PROTECTED = {"preference", "decision"}
DEFAULTS = {"enabled": False, "engine": "codex", "model": None, "full_every_hours": 20,
            "max_changes": 15, "max_shrink": .5, "use_transcripts": True,
            "protected_kinds": ["preference", "decision"]}
CHILD_ENV = "DEVMEM_MAINTAIN_CHILD"  # set for engine subprocesses so their own hooks never start maintenance
ENGINE_TIMEOUT, GIT_TIMEOUT, TRANSCRIPT_BYTES = 300, 10, 40_000
FLAG_SKIP, REVERT_SKIP, USER_EDIT_GRACE, PER_MEMORY = timedelta(days=7), timedelta(days=7), timedelta(hours=2), timedelta(hours=24)
NO_CHANGE_SUMMARY = "no change: the evidence shows nothing wrong"
AGE_REASONS = {"review date has passed", "not updated in 30+ days"}
_COMMIT_RE = r"(?<![\w/.-])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w/-])"
TOKENS = re.compile(_COMMIT_RE + r"|!\d+|#\d+|\b\d{4}-\d\d-\d\d\b|\b\w+/[\w./-]+|(?<!\w)[\w.-]+/[\w./-]+|\b\d{3,}\b")
SECRET = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-(?:proj-|ant-)?[\w-]{20,}|[sr]k_(?:live|test)_\w{10,}"
                    r"|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,})|\bBearer\s+[\w.~+/=-]{16,}"
                    r"|(?:api[_-]?key|access[_-]?key|token|secret|passw(?:or)?d|pwd)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{8,}", re.I)
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
_SLASHY = re.compile(r"(?<![\w./~:-])(?:~/|\.{1,2}/|/)?(?:[\w.-]+/)+[\w.-]*\w")
_COMMIT = re.compile(_COMMIT_RE)
_BRANCH_WORD = re.compile(r"\bbranch(?:es)?\s+[`'\"]?([A-Za-z0-9_][\w./-]*)", re.I)
_BRANCH_PREFIX = re.compile(r"^(?:feature|feat|fix|bugfix|hotfix|release|chore|refactor|docs|test|dev|wip|users?|topic)/[\w./-]+$", re.I)
_REF_OK = re.compile(r"^[A-Za-z0-9_][\w./-]*$")
_STALE_WORDS = re.compile(r"\b(WIP|uncommitted|waiting)\b", re.I)
_dropped_item = {"type": "object", "additionalProperties": False, "required": ["token", "reason"], "properties": {"token": {"type": "string"}, "reason": {"type": "string"}}}
_evidence_item = {"type": "object", "additionalProperties": False, "required": ["ref", "detail"], "properties": {"ref": {"type": "string"}, "detail": {"type": "string"}}}
_PROPOSAL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action", "memory_id", "target_id", "new_title", "new_content", "new_review_after", "dropped", "evidence", "summary"], "properties": {"action": {"enum": sorted(MODEL_ACTIONS)}, "memory_id": {"type": "integer"}, "target_id": {"type": ["integer", "null"]}, "new_title": {"type": ["string", "null"]}, "new_content": {"type": ["string", "null"]}, "new_review_after": {"type": ["string", "null"]}, "dropped": {"type": "array", "items": _dropped_item}, "evidence": {"type": "array", "items": _evidence_item}, "summary": {"type": "string"}}}
_VERIFY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok", "reason"], "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}}}
# A flag is "open" until dismissed; dry-run rows are previews, never open flags.
OPEN_FLAG_SQL = "c.action='flag' AND c.applied=0 AND c.reverted_at IS NULL AND NOT EXISTS(SELECT 1 FROM maintenance_runs r WHERE r.id=c.run_id AND r.status='dry_run')"


# ---------------------------------------------------------------- config

def _number(value) -> bool: return isinstance(value, (int, float)) and not isinstance(value, bool)
_CHECKS = {
    "enabled": (lambda v: isinstance(v, bool), "a boolean"),
    "engine": (lambda v: v in ("codex", "claude"), '"codex" or "claude"'),
    "model": (lambda v: v is None or (isinstance(v, str) and bool(v.strip()) and not v.startswith("-")), "null or a model name"),
    "full_every_hours": (lambda v: _number(v) and 0 < v <= 24 * 365, "a positive number of hours"),
    "max_changes": (lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1000, "an integer from 0 to 1000"),
    "max_shrink": (lambda v: _number(v) and 0 <= v < 1, "a number from 0 up to (not including) 1"),
    "use_transcripts": (lambda v: isinstance(v, bool), "a boolean"),
    "protected_kinds": (lambda v: isinstance(v, list) and all(isinstance(k, str) and k in KINDS for k in v), "a list of memory kinds"),
}


def config_path() -> Path: return Path(os.environ.get("DEVMEM_MAINTAIN", "~/.devmem/maintain.json")).expanduser()
def _broken(reason: str) -> dict: return {**DEFAULTS, "enabled": False, "error": reason}


def config() -> dict:
    """A missing file means defaults; anything unreadable or invalid disables maintenance and carries an error."""
    try: loaded = json.loads(config_path().read_text(encoding="utf-8"))
    except FileNotFoundError: return dict(DEFAULTS)
    except ValueError as exc: return _broken(f"maintain.json is not valid JSON: {exc}")
    except OSError as exc: return _broken(f"maintain.json unreadable: {type(exc).__name__}")
    if not isinstance(loaded, dict): return _broken("maintain.json must be a JSON object")
    for key, value in loaded.items():
        if key not in _CHECKS: return _broken(f"maintain.json: unknown key {key!r}")
        valid, expected = _CHECKS[key]
        if not valid(value): return _broken(f"maintain.json: {key} must be {expected}")
    cfg = {**DEFAULTS, **loaded}
    cfg["protected_kinds"] = sorted(set(cfg["protected_kinds"]) | ALWAYS_PROTECTED)
    return cfg


# ---------------------------------------------------------------- engine

class EngineUnavailable(RuntimeError):
    """The engine cannot run at all (missing binary, bad name); no single memory is to blame."""


def _tail(text: str | None, size: int = 400) -> str: return SECRET.sub("[redacted]", (text or "").strip()[-size:])
def _strip_fences(text: str) -> str: return re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text)


def _engine_cwd(payload: dict) -> Path:
    project = payload.get("memory", {}).get("project") if isinstance(payload.get("memory"), dict) else None
    return Path(project).expanduser() if isinstance(project, str) and Path(project).expanduser().is_dir() else Path.home()


class Engine:
    """Production engine boundary; tests supply a small fake with proposal/verify.

    The prompt goes on stdin (never argv, never an error message) and errors are sanitized,
    so memory text and transcripts cannot leak into maintenance_runs.error."""
    def __init__(self, name="codex", model=None): self.name, self.model = name, model

    def _run(self, prompt: str, payload: dict, schema: dict) -> dict:
        cwd, env = _engine_cwd(payload), {**os.environ, CHILD_ENV: "1"}
        prompt = "<devmem-spawned>\n" + prompt
        with tempfile.TemporaryDirectory(prefix="devmem-maintain-") as directory:
            schema_path, output_path = Path(directory) / "schema.json", Path(directory) / "output.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            if self.name == "codex":
                command = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(cwd)] + (["--model", self.model] if self.model else [])
                command += ["--output-schema", str(schema_path), "-o", str(output_path), "-"]
            elif self.name == "claude":
                command = ["claude", "-p", "--output-format", "json"] + (["--model", self.model] if self.model else [])
                prompt += "\n\nReply with exactly one JSON object matching this JSON schema, no prose:\n" + json.dumps(schema)
            else: raise EngineUnavailable("engine must be codex or claude")
            try: result = subprocess.run(command, input=prompt, cwd=cwd, env=env, text=True, capture_output=True, timeout=ENGINE_TIMEOUT)
            except FileNotFoundError: raise EngineUnavailable(f"{self.name} binary not found on PATH") from None
            except subprocess.TimeoutExpired: raise EngineRunFailed(f"{self.name} timed out after {ENGINE_TIMEOUT} s") from None
            if result.returncode: raise EngineRunFailed(f"{self.name} exited with code {result.returncode}: {_tail(result.stderr) or 'no stderr'}")
            if self.name == "codex":
                if not output_path.is_file(): raise EngineRunFailed("codex wrote no output file")
                raw = output_path.read_text(encoding="utf-8")
            else:
                try: outer = json.loads(result.stdout)
                except ValueError: raise EngineOutputError("claude returned non-JSON output") from None
                if not isinstance(outer, dict) or outer.get("is_error") or not isinstance(outer.get("result"), str):
                    raise EngineRunFailed("claude reported an error: " + _tail(str(outer.get("result") if isinstance(outer, dict) else outer), 200))
                raw = _strip_fences(outer["result"])
        try: parsed = json.loads(raw)
        except ValueError: raise EngineOutputError(f"{self.name} returned invalid JSON") from None
        if not isinstance(parsed, dict): raise EngineOutputError(f"{self.name} returned a non-object JSON result")
        return parsed

    def proposal(self, payload: dict) -> dict:
        prompt = """You are proposing conservative local-memory maintenance. Return only schema JSON.
Rules: memory_id must equal memory.id. Use only the supplied mechanical evidence and transcript lines (cite them as transcript:N exactly); entries under "context" are not evidence. Flag uncertainty; never delete.
Protected memories (memory.protected or a protected partner) are flag-only. update rewrites memory; merge keeps memory with the merged text and supersedes target_id; supersede marks memory as replaced by target_id; target_id must be one of partners.
none means the evidence shows nothing wrong (a false finding, or history that is still true); prefer it to flag when the user need not look. keep is only for an age/review-date finding. Updates, merges and supersedes need evidence. Do not shrink content by more than 50% or exceed 2,000 characters.
Every commit hash, !N/#N, ISO date, branch-like token, file path or 3+ digit number that is missing from the new text must be listed in dropped with a reason naming a mechanical evidence ref or transcript:N. Never include secrets.
A relative-date item only says the words are present; updated_at is not when they were written. Replace one only when the text itself shows which day it was and it names a past event; when it means "currently" or "these days", leave it.
Payload:
""" + json.dumps(payload, ensure_ascii=False, default=str)
        return self._run(prompt, payload, _PROPOSAL_SCHEMA)

    def verify(self, payload: dict) -> dict:
        prompt = "Return only schema JSON. Is every claim in the new text supported by the evidence, and is nothing true from the old text lost without a stated reason? Reject unsupported claims or unexplained loss. Payload:\n" + json.dumps(payload, ensure_ascii=False, default=str)
        return self._run(prompt, payload, _VERIFY_SCHEMA)


# ---------------------------------------------------------------- lock, schedule, transcript

@dataclass
class Lock:
    """flock on a held fd: the kernel releases it when the process dies, so stale/garbled files and PID reuse cannot block."""
    path: Path
    acquired: bool = False
    fd: int | None = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: os.close(fd); return self
        except BaseException: os.close(fd); raise
        os.ftruncate(fd, 0); os.write(fd, str(os.getpid()).encode()); self.fd, self.acquired = fd, True
        return self
    def __exit__(self, *_):
        if self.fd is not None: os.close(self.fd)  # the file stays: unlinking would let a racing opener lock a dead inode
        self.fd, self.acquired = None, False


def lock_path() -> Path: return Path(os.environ.get("DEVMEM_MAINTAIN_LOCK", "~/.devmem/maintain.lock")).expanduser()
def _hidden_sql(alias="m"): return f"NOT EXISTS(SELECT 1 FROM memory_links sl WHERE sl.to_id={alias}.id AND sl.type='supersedes')"


def due_for_startup() -> bool:
    """Only an unlimited, project-less, non-dry-run successful run resets the full-run window; so does a missing engine, which retrying sooner cannot fix."""
    cfg = config()
    if not cfg["enabled"] and not cfg.get("error"): return False
    with connection() as conn:
        row = conn.execute("""SELECT finished_at FROM maintenance_runs WHERE full=1 AND finished_at IS NOT NULL
            AND (status='success' OR (status='error' AND error LIKE 'engine unavailable:%')) ORDER BY id DESC LIMIT 1""").fetchone()
    finished = parse_ts(row["finished_at"]) if row else None
    return not finished or datetime.now(UTC) - finished > timedelta(hours=float(cfg["full_every_hours"]))


def _transcript_text(line: str) -> str:
    try: item = json.loads(line)
    except ValueError: return ""
    if not isinstance(item, dict): return ""
    message = item.get("message")
    role, content = (message.get("role"), message.get("content")) if isinstance(message, dict) else (item.get("role") or item.get("type"), item.get("content"))
    if role not in {"user", "assistant"}: return ""
    if isinstance(content, str): return content.strip()
    if isinstance(content, list): return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text").strip()
    return ""


def read_transcript(path: str | None) -> tuple[str | None, str | None]:
    """Return (excerpt, note): only user/assistant text, newest ~40 KB of whole `transcript:N` lines."""
    if not path: return None, None
    try: lines = Path(path).expanduser().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc: return None, f"transcript unavailable ({type(exc).__name__})"
    pieces = [f"transcript:{number} {text}" for number, line in enumerate(lines, 1) if (text := _transcript_text(line))]
    kept, size = [], 0
    for piece in reversed(pieces):
        size += len(piece.encode()) + 1
        if size > TRANSCRIPT_BYTES:
            if not kept:  # the newest line alone is too long: keep its citation prefix and its tail
                prefix, _, text = piece.partition(" ")
                kept.append(prefix + " …" + text.encode()[-(TRANSCRIPT_BYTES - 200):].decode(errors="ignore"))
            break
        kept.append(piece)
    excerpt = "\n".join(reversed(kept))
    return (excerpt, None) if excerpt else (None, "transcript has no user or assistant text")


def transcript_excerpt(path: str | None) -> str | None: return read_transcript(path)[0]


# ---------------------------------------------------------------- mechanical evidence

class _GitError(Exception): pass


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL, env=env)


def _git_ok(project: Path, *args: str) -> bool:
    """rc 0 → True, rc 1 → False; anything else is a check error, never evidence."""
    result = _git(project, *args)
    if result.returncode in (0, 1): return result.returncode == 0
    raise _GitError(f"git {args[0]} exited {result.returncode}: {_tail(result.stderr, 200)}")


def _default_branch(project: Path) -> str | None:
    head = _git(project, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        candidate = head.stdout.strip().removeprefix("refs/remotes/")
        if _git_ok(project, "rev-parse", "--verify", "--quiet", "--end-of-options", candidate + "^{commit}"): return candidate
    for name in ("main", "master"):
        if _git_ok(project, "rev-parse", "--verify", "--quiet", "--end-of-options", f"refs/heads/{name}"): return name
    return None


def _clean(token: str) -> str: return token.strip("`'\"").rstrip(".,;:)")


def _branch_check(project: Path, branch: str, base: str | None, strong: bool) -> tuple[dict | None, dict | None]:
    """Return (evidence, context) for one branch name."""
    if not _REF_OK.match(branch) or ".." in branch or branch.endswith((".", "/", ".lock")): return None, None
    if not _git_ok(project, "rev-parse", "--verify", "--quiet", "--end-of-options", branch + "^{commit}"):
        return ({"ref": branch, "check": "branch", "result": "missing", "command": f"git rev-parse --verify {branch}"} if strong else None), None
    if not base or branch == base or branch == base.rsplit("/", 1)[-1]: return None, None
    if _git(project, "rev-parse", "--end-of-options", branch).stdout == _git(project, "rev-parse", "--end-of-options", base).stdout: return None, None
    if not _git_ok(project, "merge-base", "--is-ancestor", "--end-of-options", branch, base): return None, None
    merges = _git(project, "rev-list", "--ancestry-path", "--merges", "--reverse", "--end-of-options", f"{branch}..{base}").stdout.split()
    if merges:
        commit = _git(project, "rev-parse", "--short", "--end-of-options", merges[0]).stdout.strip()
        return {"ref": branch, "check": "branch", "result": "merged", "command": f"git merge-base --is-ancestor {branch} {base}", "commit": commit, "base": base}, None
    return None, {"ref": branch, "check": "branch", "result": f"contained in {base} without a merge commit (not evidence)", "command": f"git merge-base --is-ancestor {branch} {base}"}


def _tracked_suffixes(project: Path) -> set[str]:
    """Every tracked file and directory path plus each of its trailing segments, so core/settings.py matches packages/x/src/x/core/settings.py."""
    result = _git(project, "ls-files", "-z")
    if result.returncode != 0: raise _GitError(f"git ls-files exited {result.returncode}: {_tail(result.stderr, 200)}")  # an empty set would make every path "missing"
    out = set()
    for path in filter(None, result.stdout.split("\0")):
        parts = path.split("/")
        for end in range(1, len(parts) + 1):
            for start in range(end): out.add("/".join(parts[start:end]))
    return out


def _path_exists(project: Path, token: str, repo: bool) -> bool:
    if token.startswith(("~", "/")): return Path(token).expanduser().exists()
    if (project / token).exists() or (project.parent / token).exists(): return True  # project.parent: notes such as knowledge-library/ beside the repos
    if not repo: return False
    key = str(project)
    if key not in _SUFFIX_CACHE: _SUFFIX_CACHE[key] = _tracked_suffixes(project)
    return token.strip("/") in _SUFFIX_CACHE[key]


_SUFFIX_CACHE: dict[str, set[str]] = {}


def _evidence(memory: dict) -> tuple[list[dict], list[dict]]:
    """Return (evidence, context): failing mechanical checks, and informational items/check errors that never count."""
    text, out, info = memory["title"] + "\n" + memory["content"], [], []
    age = parse_ts(memory["updated_at"])
    if age and datetime.now(UTC) - age > timedelta(days=3) and _STALE_WORDS.search(text):
        out.append({"ref": "wording", "check": "age", "result": "stale status", "command": "timestamp comparison"})
    if not memory["project"]: return out, info  # a global memory has no project directory to check against
    project = Path(memory["project"]).expanduser()
    if not project.is_dir():
        info.append({"ref": str(project), "check": "error", "result": "project directory missing", "command": "filesystem exists"}); return out, info
    scan = _URL.sub(" ", text)
    branch_words = {_clean(word) for word in _BRANCH_WORD.findall(scan)}
    repo, base, git_failed = (project / ".git").exists(), None, False
    def broken(exc):
        nonlocal repo, git_failed
        repo, git_failed = False, True; info.append({"ref": str(project), "check": "error", "result": f"git check failed: {type(exc).__name__}: {_tail(str(exc), 200)}", "command": "git"})
    if repo:
        try:
            if not _git_ok(project, "rev-parse", "--git-dir"): raise _GitError("not a git repository")
            base = _default_branch(project)
            if not base: info.append({"ref": str(project), "check": "error", "result": "no default branch (origin/HEAD, main or master)", "command": "git symbolic-ref"})
        except (_GitError, OSError, subprocess.TimeoutExpired) as exc: broken(exc)
    branches: dict[str, bool] = {word: bool(re.search(r"[/\-_\d]", word)) for word in branch_words}
    for token in dict.fromkeys(_clean(t) for t in _SLASHY.findall(scan)):
        if not token or re.fullmatch(r"[\d/.\-]+", token): continue  # dates and ratios such as 10/28
        first, last = token.split("/", 1)[0], token.rsplit("/", 1)[-1]
        if token.startswith(("/", "~/", "./", "../")) or re.search(r"\.[A-Za-z][A-Za-z0-9]{0,7}$", last) or (project / first).exists(): kind = "path"
        elif _BRANCH_PREFIX.match(token) or token in branch_words: kind = "branch"
        else: kind = "maybe-branch"  # "and/or", "Q3/Q4": only checked if it is a real ref
        if kind == "path":
            if token.startswith("/") and not re.search(r"\.[A-Za-z][A-Za-z0-9]{0,7}$", last) and not Path("/" + token.split("/")[1]).exists(): continue  # a route such as /v1/extract
            try: exists = _path_exists(project, token, repo)
            except (_GitError, OSError, subprocess.TimeoutExpired) as exc: broken(exc); exists = False
            if exists: continue
            # fix(2026-10-28): with git broken a repo-relative path cannot be checked against tracked files, so it is context, never "missing" evidence.
            if git_failed and not token.startswith(("~", "/")):
                info.append({"ref": token, "check": "path", "result": "unverifiable: not on disk and git failed", "command": "filesystem exists"})
            else:
                out.append({"ref": token, "check": "path", "result": "missing", "command": "filesystem exists, git ls-files suffix, project parent"})
        else: branches.setdefault(token, kind == "branch")
    if repo:
        try:
            for branch, strong in branches.items():
                found, context = _branch_check(project, branch, base, strong)
                if found: out.append(found)
                if context: info.append(context)
            for commit in dict.fromkeys(_COMMIT.findall(scan)):
                if not _git_ok(project, "rev-parse", "--verify", "--quiet", "--end-of-options", commit + "^{commit}"):
                    out.append({"ref": commit, "check": "commit", "result": "missing", "command": "git rev-parse --verify " + commit}); continue
                newer = _git(project, "log", "-1", "--format=%h", "--end-of-options", commit + "..HEAD")
                if newer.returncode == 0 and newer.stdout.strip():
                    # Context only: some later commit on HEAD is nearly always true and says nothing about the memory being stale.
                    info.append({"ref": commit, "check": "commit", "result": "newer commit exists", "command": f"git log -1 {commit}..HEAD", "commit": newer.stdout.strip()})
        except (_GitError, OSError, subprocess.TimeoutExpired) as exc: broken(exc)
    return out, info


# ---------------------------------------------------------------- guardrails

def _protected(memory: dict, cfg: dict) -> bool:
    return memory["kind"] in ALWAYS_PROTECTED | set(cfg["protected_kinds"]) or "no-auto" in re.split(r"[\s,]+", (memory["tags"] or "").lower())
def _superseded(conn, memory_id: int) -> bool: return bool(conn.execute("SELECT 1 FROM memory_links WHERE to_id=? AND type='supersedes'", (memory_id,)).fetchone())


def _applied_within(conn, memory_id: int, window: timedelta, column: str) -> bool:
    """Applied automatic changes only: a dismissed flag is not a revert."""
    cutoff = datetime.now(UTC) - window
    return any((stamp := parse_ts(row[0])) and stamp > cutoff for row in conn.execute(f"SELECT {column} FROM maintenance_changes WHERE applied=1 AND (memory_id=? OR target_id=?)", (memory_id, memory_id)))


def _auto_edited_after(conn, memory: dict) -> bool:
    updated = parse_ts(memory["updated_at"])
    return bool(updated and any((stamp := parse_ts(row[0])) and stamp >= updated for row in conn.execute("SELECT created_at FROM maintenance_changes WHERE memory_id=? AND applied=1", (memory["id"],))))


def _mentions(text: str, ref: str) -> bool: return bool(ref) and bool(re.search(r"(?<![\w/.-])" + re.escape(ref) + r"(?![\w/-])(?!\.\w)", text))
def _tokens(text: str) -> set[str]: return {token.rstrip(".,;:") for token in TOKENS.findall(text)} - {""}
def _cited(proposal: dict, ctx: dict) -> set[str]:
    return {ref for item in proposal.get("evidence") or [] if isinstance(item, dict) and isinstance(ref := item.get("ref"), str)
            and (m := re.fullmatch(r"transcript:(\d+)", ref.strip())) and m.group(1) in ctx["citations"]}


def _blocked(memory: dict, proposal: dict, ctx: dict, conn) -> tuple[str | None, dict | None]:
    """Return (guardrail name or None, target row). ctx holds only code-produced facts; nothing the model says counts as evidence."""
    cfg, action = ctx["cfg"], proposal.get("action")
    if action not in ACTIONS: return "action", None
    if action == "flag": return None, None
    target = None
    if action in {"merge", "supersede"}:
        target_id = proposal.get("target_id")
        if not isinstance(target_id, int) or isinstance(target_id, bool) or target_id == memory["id"] or target_id not in ctx["partners"] or target_id in ctx["handled"]: return "target", None
        row = conn.execute("SELECT * FROM memories WHERE id=?", (target_id,)).fetchone()
        if not row or row["project"] != memory["project"] or _superseded(conn, target_id): return "target", None
        target = dict(row)
    if _protected(memory, cfg) or (target and _protected(target, cfg)): return "protected", target  # every writing action, keep included
    for changed in (memory, target):
        if not changed: continue
        if changed["id"] in ctx["handled"] or _applied_within(conn, changed["id"], PER_MEMORY, "created_at"): return "per_memory_limit", target
        if _applied_within(conn, changed["id"], REVERT_SKIP, "reverted_at"): return "recent_revert", target
        edited = parse_ts(changed["updated_at"])
        if edited and datetime.now(UTC) - edited < USER_EDIT_GRACE and not _auto_edited_after(conn, changed): return "recent_edit", target
    if action == "keep": return (None if ctx["age_only"] else "keep_reason"), target
    # "relative-date" and "wording" only say some words are present; they never justify dropping a token.
    refs = {item["ref"] for item in ctx["evidence"] if item["check"] not in ("relative-date", "age")} | _cited(proposal, ctx)
    if not ctx["evidence"] and not _cited(proposal, ctx) and not (target and target["id"] in ctx["duplicates"]): return "evidence", target
    if action == "supersede": return None, target
    content, title = proposal.get("new_content"), proposal.get("new_title")
    if not isinstance(content, str) or not content.strip(): return "content", target
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 200): return "content", target
    try: validate_content(content)
    except ValueError: return "size", target
    try: validate_review_after(proposal.get("new_review_after"))
    except ValueError: return "review_after", target
    old = memory["title"] + "\n" + memory["content"] + ("\n" + target["title"] + "\n" + target["content"] if target else "")
    if len(content) < (len(memory["content"]) + (len(target["content"]) if target else 0)) * (1 - float(cfg["max_shrink"])): return "shrink", target
    dropped = {d.get("token"): d.get("reason") for d in proposal.get("dropped") or [] if isinstance(d, dict)}
    for token in _tokens(old) - _tokens(content + "\n" + (title or memory["title"])):
        reason = dropped.get(token)
        if not isinstance(reason, str) or not any(_mentions(reason, ref) for ref in refs): return "information", target
    return None, target


def _has_secret(proposal: dict) -> bool:
    texts = [proposal.get(key) for key in ("new_title", "new_content", "summary", "new_review_after")]
    for key in ("evidence", "dropped"):
        for item in proposal.get(key) or []: texts += list(item.values()) if isinstance(item, dict) else [item]
    return any(SECRET.search(value if isinstance(value, str) else json.dumps(value, default=str)) for value in texts if value is not None)


# ---------------------------------------------------------------- records and apply

def _record(conn, run_id, memory, proposal, evidence, applied=False, blocked=None, revision_id=None, link_id=None, old_review_after=None, new_review_after=None):
    text = lambda key: proposal.get(key) if isinstance(proposal.get(key), str) else None
    target = proposal.get("target_id") if isinstance(proposal.get("target_id"), int) and not isinstance(proposal.get("target_id"), bool) else None
    action = proposal.get("action") if proposal.get("action") in ACTIONS and (applied or blocked == "dry_run") else "flag"
    summary = str(proposal.get("summary") or "flagged")[:500]
    if action == "flag" and proposal.get("action") in ACTIONS - {"flag"}: summary = f"[{proposal['action']}] {summary}"
    return conn.execute("""INSERT INTO maintenance_changes(run_id,memory_id,target_id,action,applied,summary,evidence,old_title,old_content,new_title,new_content,revision_id,link_id,blocked_by,old_review_after,new_review_after,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (run_id, memory["id"], target, action, int(applied), summary, json.dumps(evidence, default=str), memory["title"], memory["content"],
        text("new_title"), text("new_content"), revision_id, link_id, blocked, old_review_after, new_review_after, now())).lastrowid


def _evidence_for_record(code: list[dict], proposal: dict) -> list[dict]:
    model = [{"ref": str(item.get("ref", ""))[:200], "detail": str(item.get("detail", ""))[:500], "source": "model"} for item in proposal.get("evidence") or [] if isinstance(item, dict)]
    return code + model


def _relative_dates(memory: dict) -> dict | None:
    """Evidence item, never an edit: "today" often means "currently", and updated_at is not when the words were written."""
    found = [word for word in ("today", "yesterday", "this week") if re.search(r"\b" + word + r"\b", memory["content"], re.I)]
    if not found: return None
    return {"ref": "relative-date", "check": "relative-date", "result": "relative date words present: " + ", ".join(found), "command": "text scan"}


def _apply(conn, run_id, memory, target, proposal, evidence) -> None:
    action, revision_id, link_id, new_review = proposal["action"], None, None, None
    if action in {"update", "merge"}:
        before = conn.execute("SELECT COALESCE(MAX(id),0) FROM memory_revisions WHERE memory_id=?", (memory["id"],)).fetchone()[0]
        new_review = validate_review_after(proposal.get("new_review_after"))
        conn.execute("UPDATE memories SET title=?,content=?,review_after=?,updated_at=? WHERE id=?", (proposal.get("new_title") or memory["title"], proposal["new_content"], new_review or memory["review_after"], now(), memory["id"]))
        # Only a revision written by this UPDATE; an older unrelated one would make revert wipe a later user edit.
        revision_id = conn.execute("SELECT MAX(id) FROM memory_revisions WHERE memory_id=? AND id>?", (memory["id"], before)).fetchone()[0]
    if action in {"merge", "supersede"}:
        newer, older = (memory["id"], target["id"]) if action == "merge" else (target["id"], memory["id"])
        link_id = conn.execute("INSERT INTO memory_links(from_id,to_id,type,note,created_at) VALUES(?,?,'supersedes','automatic maintenance',?)", (newer, older, now())).lastrowid
    if action == "keep":
        new_review = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
        conn.execute("UPDATE memories SET review_after=? WHERE id=?", (new_review, memory["id"]))
    _record(conn, run_id, memory, proposal, evidence, applied=True, revision_id=revision_id, link_id=link_id, old_review_after=memory["review_after"], new_review_after=new_review)


def _skip_ids(conn) -> set[int]:
    """Memories with a recent real flag they have not been edited since (dedupe), or a recent engine error (retry later)."""
    cutoff, out = datetime.now(UTC) - FLAG_SKIP, set()
    for row in conn.execute("""SELECT c.memory_id,c.created_at,c.blocked_by,c.reverted_at,m.updated_at FROM maintenance_changes c JOIN memories m ON m.id=c.memory_id
        WHERE c.action='flag' AND c.applied=0 AND NOT EXISTS(SELECT 1 FROM maintenance_runs r WHERE r.id=c.run_id AND r.status='dry_run')"""):
        flagged, edited = parse_ts(row["created_at"]), parse_ts(row["updated_at"])
        if not flagged or flagged <= cutoff: continue
        if (row["reverted_at"] is None) if row["blocked_by"] == "engine_error" else (not edited or edited <= flagged): out.add(row["memory_id"])
    return out


class _MemoryFailed(Exception):
    def __init__(self, memory_id: int, message: str): super().__init__(message); self.memory_id = memory_id


class EngineRunFailed(RuntimeError):
    """The engine process failed, so the run rather than one memory failed."""


class EngineOutputError(RuntimeError):
    """One engine response could not be used for this memory."""


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, (KeyboardInterrupt, SystemExit)): return f"interrupted ({type(exc).__name__})"
    if isinstance(exc, (_MemoryFailed, EngineUnavailable, EngineRunFailed, EngineOutputError)) or type(exc) is RuntimeError: return _tail(str(exc), 500)  # already sanitized by Engine
    return _tail(f"{type(exc).__name__}: {exc}", 500)


def _call_engine(memory_id: int, fn, payload: dict) -> dict:
    try: result = fn(payload)
    except EngineOutputError as exc: raise _MemoryFailed(memory_id, f"memory #{memory_id}: {_error_text(exc)}") from None
    if not isinstance(result, dict): raise _MemoryFailed(memory_id, f"memory #{memory_id}: engine returned a non-object result")
    return result


def _shape_problem(proposal: dict, memory_id: int) -> str | None:
    if proposal.get("memory_id") != memory_id: return "engine proposal has the wrong memory_id"
    if not isinstance(proposal.get("evidence", []), list) or not isinstance(proposal.get("dropped", []), list): return "engine proposal has invalid evidence or dropped lists"
    return None


def _candidates(conn, project) -> tuple[dict[int, dict], dict[int, list[dict]], dict[int, set[int]]]:
    audit_by_id, duplicates, today = {}, {}, datetime.now(UTC).date().isoformat()
    for finding in audit_memories(conn):
        if finding.get("other_id"):
            duplicates.setdefault(finding["id"], set()).add(finding["other_id"]); duplicates.setdefault(finding["other_id"], set()).add(finding["id"])
        audit_by_id.setdefault(finding["id"], []).append(finding)
    where, params = _hidden_sql(), []
    if project: where += " AND m.project=?"; params.append(project)
    rows = {r["id"]: dict(r) for r in conn.execute(f"SELECT m.* FROM memories m WHERE {where}", params)}
    for memory_id, findings in list(audit_by_id.items()):
        memory = rows.get(memory_id)
        # A memory reviewed into the future is not "old" again until that date.
        kept = [f for f in findings if memory and not (f["reason"] == "not updated in 30+ days" and memory["review_after"] and memory["review_after"] >= today)]
        if kept: audit_by_id[memory_id] = kept
        else: del audit_by_id[memory_id]
    selected = {mid: row for mid, row in sorted(rows.items()) if row["kind"] in ("todo", "note") or row["pinned"] or mid in audit_by_id}
    return selected, audit_by_id, duplicates


def _partners(conn, memory: dict, duplicate_ids: set[int]) -> dict[int, dict]:
    linked = {row[0] for row in conn.execute("SELECT CASE WHEN from_id=? THEN to_id ELSE from_id END FROM memory_links WHERE (from_id=? OR to_id=?) AND type IN ('overlaps','relates_to')", (memory["id"],) * 3)}
    ids = sorted((duplicate_ids | linked) - {memory["id"]})
    if not ids: return {}
    rows = conn.execute(f"SELECT m.* FROM memories m WHERE m.id IN ({','.join('?' * len(ids))}) AND m.project IS ? AND {_hidden_sql()}", (*ids, memory["project"]))
    return {row["id"]: dict(row) for row in rows}


def _gather(project, limit, engine, transcript, cfg, stats: dict) -> tuple[list[dict], int]:
    """Collect proposals and verdicts with no write transaction open (engine calls can take minutes)."""
    citations = set(re.findall(r"^transcript:(\d+) ", transcript or "", re.M))
    _SUFFIX_CACHE.clear()  # a repo's files change between runs in a long-lived process
    decisions, handled, accepted, deferred = [], set(), 0, 0
    with connection() as conn:
        candidates, audit_by_id, duplicates = _candidates(conn, project); skip = _skip_ids(conn)
        for memory in candidates.values():
            if memory["id"] in handled or memory["id"] in skip or _superseded(conn, memory["id"]): continue
            if _applied_within(conn, memory["id"], PER_MEMORY, "created_at"): continue  # the per-memory limit would block any change: don't spend a model call
            if limit and stats["checked"] >= limit: break
            stats["checked"] += 1
            evidence, info = _evidence(memory); audited = audit_by_id.get(memory["id"], [])
            if relative := _relative_dates(memory): evidence = evidence + [relative]
            if not evidence and not audited: continue
            if accepted >= int(cfg["max_changes"]): deferred += 1; continue  # no engine calls once the run is full
            partners = _partners(conn, memory, duplicates.get(memory["id"], set()))
            age_only = not any(item["check"] in {"branch", "commit", "path", "relative-date"} for item in evidence) and all(item["reason"] in AGE_REASONS for item in audited)
            code_evidence = evidence + [{"ref": "audit", "check": "audit", "result": item["reason"], "command": "audit_memories"} for item in audited] + info
            payload = {"memory": {**memory, "protected": _protected(memory, cfg)}, "partners": [{**p, "protected": _protected(p, cfg)} for p in partners.values()],
                       "evidence": evidence, "context": code_evidence[len(evidence):], "transcript": transcript}
            proposal = _call_engine(memory["id"], engine.proposal, payload)
            if problem := _shape_problem(proposal, memory["id"]): raise _MemoryFailed(memory["id"], f"memory #{memory['id']}: {problem}")
            decision = {"memory": memory, "proposal": proposal, "evidence": code_evidence, "target": None, "blocked": None,
                        "ctx": {"cfg": cfg, "evidence": evidence, "citations": citations, "partners": partners, "duplicates": duplicates.get(memory["id"], set()), "handled": set(handled), "age_only": age_only}}
            if _has_secret(proposal):  # first: a "none" proposal is stored too and must not carry a secret either
                decision.update(blocked="secret", proposal={"action": "flag", "memory_id": memory["id"], "summary": "proposal dropped: it contained secret-like text"}); decisions.append(decision); continue
            if proposal.get("action") == "none":
                # Stored as a fixed summary with no new text: nobody sees the row, so nothing model-written is kept in it.
                decisions.append({**decision, "blocked": "no_change", "proposal": {"action": "none", "memory_id": memory["id"], "summary": NO_CHANGE_SUMMARY}}); continue
            decision["blocked"], decision["target"] = _blocked(memory, proposal, decision["ctx"], conn)
            if not decision["blocked"] and proposal["action"] in {"update", "merge", "supersede"}:
                target = decision["target"]; join = lambda *rows: "\n\n".join(r["title"] + "\n" + r["content"] for r in rows if r)
                old_text, new_text = (join(memory), join(target)) if proposal["action"] == "supersede" else (join(memory, target), (proposal.get("new_title") or memory["title"]) + "\n" + proposal["new_content"])
                cited = [line for line in (transcript or "").splitlines() if any(line.startswith(ref + " ") for ref in _cited(proposal, decision["ctx"]))]
                verdict = _call_engine(memory["id"], engine.verify, {"memory": {"project": memory["project"]}, "old_text": old_text, "new_text": new_text, "mechanical_evidence": evidence,
                                                                      "model_evidence": proposal.get("evidence") or [], "dropped": proposal.get("dropped") or [], "transcript_lines": cited})
                if verdict.get("ok") is not True: decision["blocked"] = "verifier"
            decisions.append(decision)
            if not decision["blocked"] and proposal["action"] != "flag":
                accepted += 1; handled.add(memory["id"])
                if decision["target"]: handled.add(decision["target"]["id"])
    return decisions, deferred


def _apply_all(run_id, decisions, dry_run, cfg, checked, note) -> dict:
    """Every write of the run in one short IMMEDIATE transaction, re-checking guards against fresh rows."""
    applied = flagged = proposed = unchanged = 0; done: set[int] = set(); preview = []
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for decision in decisions:
                row = conn.execute("SELECT * FROM memories WHERE id=?", (decision["memory"]["id"],)).fetchone()
                if not row: continue  # deleted while the run was thinking
                memory, proposal, blocked, target = dict(row), decision["proposal"], decision["blocked"], decision["target"]
                if not blocked and proposal["action"] != "flag":
                    fresh_target = target and conn.execute("SELECT * FROM memories WHERE id=?", (target["id"],)).fetchone()
                    if memory["updated_at"] != decision["memory"]["updated_at"] or (target and (not fresh_target or fresh_target["updated_at"] != target["updated_at"])): blocked = "recent_edit"
                    else: blocked, target = _blocked(memory, proposal, {**decision["ctx"], "handled": done}, conn)
                    if not blocked and applied + proposed >= int(cfg["max_changes"]): blocked = "max_changes"
                if blocked == "no_change":
                    change_id = _record(conn, run_id, memory, {**proposal, "action": "flag"}, decision["evidence"], blocked="no_change"); unchanged += 1
                    conn.execute("UPDATE maintenance_changes SET reverted_at=?, seen_at=? WHERE id=?", (now(), now(), change_id))  # closed at once: nothing for the user to see
                elif blocked or proposal["action"] == "flag":
                    if not dry_run: conn.execute("UPDATE maintenance_changes SET reverted_at=? WHERE memory_id=? AND action='flag' AND applied=0 AND reverted_at IS NULL", (now(), memory["id"]))  # one open flag per memory
                    evidence = decision["evidence"] if blocked == "secret" else _evidence_for_record(decision["evidence"], proposal)
                    _record(conn, run_id, memory, proposal, evidence, blocked=blocked or "flag"); flagged += 1
                else:
                    if dry_run: _record(conn, run_id, memory, proposal, _evidence_for_record(decision["evidence"], proposal), blocked="dry_run"); proposed += 1
                    else: _apply(conn, run_id, memory, target, proposal, _evidence_for_record(decision["evidence"], proposal)); applied += 1
                    done.update({memory["id"]} | ({target["id"]} if target else set()))
                preview.append({"memory_id": memory["id"], "action": proposal.get("action"), "target_id": proposal.get("target_id"), "blocked_by": blocked or ("flag" if proposal.get("action") == "flag" else None),
                                "summary": proposal.get("summary"), "new_title": proposal.get("new_title") if blocked != "secret" else None, "new_content": proposal.get("new_content") if blocked != "secret" else None})
            status = "dry_run" if dry_run else "success"
            conn.execute("UPDATE maintenance_runs SET finished_at=?,status=?,checked=?,applied=?,flagged=?,note=? WHERE id=?", (now(), status, checked, applied, flagged, note, run_id))
            conn.commit()
        except BaseException:
            conn.rollback(); raise
    result = {"status": status, "checked": checked, "applied": applied, "flagged": flagged, "unchanged": unchanged, "run_id": run_id}
    if dry_run: result.update(proposed=proposed, changes=preview)
    if note: result["note"] = note
    return result


def _run_locked(trigger, project, dry_run, limit, engine, transcript_path, cfg) -> dict:
    full = int(project is None and not limit and not dry_run)
    with connection() as conn:
        run_id = conn.execute("INSERT INTO maintenance_runs(trigger,project,started_at,status,engine,model,full) VALUES(?,?,?,?,?,?,?)", (trigger, project, now(), "running", engine.name, engine.model, full)).lastrowid
    stats = {"checked": 0}
    try:
        transcript, note = read_transcript(transcript_path) if transcript_path and cfg["use_transcripts"] else (None, None)
        decisions, deferred = _gather(project, limit, engine, transcript, cfg, stats)
        if deferred: note = "; ".join(filter(None, [note, f"max_changes reached: {deferred} memories left for the next run"]))
        return _apply_all(run_id, decisions, dry_run, cfg, stats["checked"], note)
    except BaseException as exc:
        message = _error_text(exc)
        if isinstance(exc, EngineUnavailable): message = "engine unavailable: " + message
        with connection() as conn:
            conn.execute("UPDATE maintenance_runs SET finished_at=?,status='error',checked=?,applied=0,flagged=0,error=? WHERE id=?", (now(), stats["checked"], message, run_id))
            if isinstance(exc, _MemoryFailed) and not dry_run:  # remembered so the next run skips it instead of failing again
                row = conn.execute("SELECT * FROM memories WHERE id=?", (exc.memory_id,)).fetchone()
                if row:
                    conn.execute("UPDATE maintenance_changes SET reverted_at=? WHERE memory_id=? AND action='flag' AND applied=0 AND reverted_at IS NULL", (now(), exc.memory_id))
                    _record(conn, run_id, dict(row), {"action": "flag", "summary": "engine failed on this memory; skipped for 7 days or until dismissed"}, [], blocked="engine_error")
        if not isinstance(exc, Exception): raise
        return {"status": "error", "checked": stats["checked"], "applied": 0, "flagged": 0, "error": message, "run_id": run_id}


def _failed_run(trigger, project, cfg, message) -> dict:
    with connection() as conn:
        run_id = conn.execute("INSERT INTO maintenance_runs(trigger,project,started_at,finished_at,status,engine,model,error) VALUES(?,?,?,?,?,?,?,?)", (trigger, project, now(), now(), "error", cfg["engine"], cfg["model"], message)).lastrowid
    return {"status": "error", "checked": 0, "applied": 0, "flagged": 0, "error": message, "run_id": run_id}


def _request(trigger, project, dry_run, limit, transcript_path, cfg) -> dict:
    """A run is active: remember one real request per (trigger, project). Dry or limited runs are never queued."""
    result = {"status": "locked", "checked": 0, "applied": 0, "flagged": 0}
    if dry_run or limit: return result
    options = json.dumps({"transcript": transcript_path}) if transcript_path else None
    with connection() as conn:
        existing = conn.execute("SELECT id FROM maintenance_runs WHERE status='requested' AND trigger=? AND project IS ?", (trigger, project)).fetchone()
        if existing:
            if options: conn.execute("UPDATE maintenance_runs SET options=? WHERE id=?", (options, existing["id"]))
        else: conn.execute("INSERT INTO maintenance_runs(trigger,project,started_at,status,engine,model,options) VALUES(?,?,?,?,?,?,?)", (trigger, project, now(), "requested", cfg["engine"], cfg["model"], options))
    return {**result, "queued": True}


def _drain(engine, cfg) -> None:
    for _ in range(20):  # bounded: requests arriving forever must not keep one process alive forever
        with connection() as conn: pending = conn.execute("SELECT * FROM maintenance_runs WHERE status='requested' ORDER BY id LIMIT 1").fetchone()
        if not pending: return
        if pending["trigger"] == "startup" and not due_for_startup(): status = "skipped"
        else:
            try: options = json.loads(pending["options"] or "{}")
            except ValueError: options = {}
            _run_locked(pending["trigger"], pending["project"], False, None, engine, options.get("transcript"), cfg); status = "done"
        with connection() as conn: conn.execute("UPDATE maintenance_runs SET status=?,finished_at=? WHERE id=? AND status='requested'", (status, now(), pending["id"]))


def run(trigger="manual", project=None, dry_run=False, limit=None, engine=None, transcript_path=None) -> dict:
    if trigger not in TRIGGERS: raise ValueError("invalid maintenance trigger")
    cfg = config()
    if not dry_run: prune_messages()  # fix(2026-10-28): also when maintenance is disabled or misconfigured
    if cfg.get("error"): return _failed_run(trigger, project, cfg, cfg["error"])
    if not cfg["enabled"]: return {"status": "disabled", "checked": 0, "applied": 0, "flagged": 0}
    try:
        with Lock(lock_path()) as lock:
            if not lock.acquired: return _request(trigger, project, dry_run, limit, transcript_path, cfg)
            with connection() as conn:  # we hold the lock, so any 'running' row belongs to a process that died
                conn.execute("UPDATE maintenance_runs SET status='error',finished_at=?,error='interrupted: the process ended before the run finished' WHERE status='running'", (now(),))
            engine = engine or Engine(cfg["engine"], cfg["model"])
            result = _run_locked(trigger, project, dry_run, limit, engine, transcript_path, cfg)
            if not dry_run: _drain(engine, cfg)  # queued requests are real runs; a dry run must change nothing
            return result
    except OSError as exc:
        return _failed_run(trigger, project, cfg, f"maintenance lock unavailable: {type(exc).__name__}: {exc}")


def _cleanup_reason(conn, flag: dict) -> str | None:
    memory = conn.execute("SELECT * FROM memories WHERE id=?", (flag["memory_id"],)).fetchone()
    if not memory: return "memory deleted"
    memory = dict(memory)
    if _superseded(conn, memory["id"]): return "memory superseded"
    flagged = parse_ts(flag["created_at"])
    edited = parse_ts(memory["updated_at"])
    if flagged and edited and edited > flagged: return "memory edited"
    if flag["blocked_by"] in {"protected", "secret", "engine_error"}: return None
    try: stored = json.loads(flag["evidence"] or "[]")
    except ValueError: stored = []
    refs = {(item.get("ref"), item.get("check")) for item in stored if isinstance(item, dict)}
    if not refs or any(not ref or check not in {"path", "branch", "commit", "age"} for ref, check in refs): return None
    current, info = _evidence(memory)
    failing = {(item.get("ref"), item.get("check")) for item in current}
    unverifiable = {(item.get("ref"), item.get("check")) for item in info}
    if not any(item.get("check") == "error" for item in info) and not refs & failing and not refs & unverifiable: return "evidence no longer holds"
    return None


def cleanup(dry_run: bool = False) -> dict:
    """Close resolved flags and discard old maintenance bookkeeping without running an engine."""
    try:
        _SUFFIX_CACHE.clear()
        with Lock(lock_path()) as lock:
            if not lock.acquired: return {"status": "locked"}
            with connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    flags = [dict(row) for row in conn.execute(f"SELECT c.* FROM maintenance_changes c WHERE {OPEN_FLAG_SQL}")]
                    reasons: dict[str, int] = {}
                    for flag in flags:
                        reason = _cleanup_reason(conn, flag)
                        if not reason: continue
                        reasons[reason] = reasons.get(reason, 0) + 1
                        if not dry_run:
                            summary = (flag["summary"] + " · closed: " + reason)[:500]
                            conn.execute("UPDATE maintenance_changes SET reverted_at=?,summary=? WHERE id=?", (now(), summary, flag["id"]))
                    old_day, old_week, old_month = "-1 day", "-7 days", "-30 days"
                    dry_runs = [row[0] for row in conn.execute("SELECT id FROM maintenance_runs WHERE status='dry_run' AND datetime(COALESCE(finished_at,started_at)) < datetime('now',?)", (old_day,))]
                    book_runs = [row[0] for row in conn.execute("SELECT id FROM maintenance_runs WHERE status IN ('requested','done','skipped') AND datetime(COALESCE(finished_at,started_at)) < datetime('now',?)", (old_day,))]
                    error_runs = [row[0] for row in conn.execute("""SELECT e.id FROM maintenance_runs e WHERE e.status='error'
                        AND datetime(COALESCE(e.finished_at,e.started_at)) < datetime('now',?) AND EXISTS(SELECT 1 FROM maintenance_runs s
                        WHERE s.status='success' AND datetime(COALESCE(s.finished_at,s.started_at)) > datetime(COALESCE(e.finished_at,e.started_at)))""", (old_week,))]
                    closed = conn.execute("""SELECT COUNT(*) FROM maintenance_changes WHERE datetime(created_at) < datetime('now',?) AND
                        ((action='flag' AND applied=0 AND reverted_at IS NOT NULL) OR (applied=1 AND reverted_at IS NOT NULL) OR blocked_by='no_change')""", (old_month,)).fetchone()[0]
                    if not dry_run:
                        run_ids = dry_runs + book_runs + error_runs
                        if run_ids:
                            marks = ",".join("?" * len(run_ids))
                            conn.execute(f"DELETE FROM maintenance_changes WHERE run_id IN ({marks})", run_ids)
                            conn.execute(f"DELETE FROM maintenance_runs WHERE id IN ({marks})", run_ids)
                        conn.execute("""DELETE FROM maintenance_changes WHERE datetime(created_at) < datetime('now',?) AND
                            ((action='flag' AND applied=0 AND reverted_at IS NOT NULL) OR (applied=1 AND reverted_at IS NOT NULL) OR blocked_by='no_change')""", (old_month,))
                    conn.commit()
                except BaseException:
                    conn.rollback(); raise
    except OSError:
        return {"status": "locked"}
    closed_flags = sum(reasons.values())
    detail = ", ".join(f"{reason}: {count}" for reason, count in reasons.items())
    line = f"closed {closed_flags} flags" + (f" ({detail})" if detail else "")
    line += f" · deleted {len(dry_runs)} dry-run runs · deleted {len(book_runs)} bookkeeping runs · deleted {len(error_runs)} error runs · deleted {closed} closed rows"
    return {"status": "dry_run" if dry_run else "ok", "line": line, "closed_flags": closed_flags, "dry_run_runs": len(dry_runs),
            "bookkeeping_runs": len(book_runs), "error_runs": len(error_runs), "closed_rows": closed}


def prune_messages() -> int:
    """Delete old closed message threads (and their subagents' timelines); never fails a run."""
    from .messages import prune  # local: messages imports SECRET from here
    try:
        with connection() as conn: return prune(conn)
    except sqlite3.Error as exc:
        logging.getLogger("devmem.maintenance").warning("message prune failed: %s: %s", type(exc).__name__, exc); return 0


def revert(change_id: int) -> bool:
    """Undo one applied change. False when there is nothing to revert; ValueError when reverting would destroy a later edit."""
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        change = conn.execute("SELECT * FROM maintenance_changes WHERE id=? AND applied=1 AND reverted_at IS NULL", (change_id,)).fetchone()
        if not change: return False
        memory = conn.execute("SELECT * FROM memories WHERE id=?", (change["memory_id"],)).fetchone()
        if change["action"] in {"update", "merge", "keep"}:
            if not memory: raise ValueError("the memory no longer exists; restore it from its history instead")
            edited, applied_at = parse_ts(memory["updated_at"]), parse_ts(change["created_at"])
            if edited and applied_at and edited > applied_at: raise ValueError("the memory was edited after this change; use its history to restore an older version")
        if change["revision_id"]: restore_memory(conn, change["memory_id"], change["revision_id"], "maintenance")
        if change["action"] in {"update", "merge", "keep"} and change["new_review_after"] is not None:
            conn.execute("UPDATE memories SET review_after=? WHERE id=?", (change["old_review_after"], change["memory_id"]))
        if change["link_id"]:
            newer, older = (change["memory_id"], change["target_id"]) if change["action"] == "merge" else (change["target_id"], change["memory_id"])
            conn.execute("DELETE FROM memory_links WHERE id=? AND from_id=? AND to_id=? AND type='supersedes'", (change["link_id"], newer, older))
        conn.execute("UPDATE maintenance_changes SET reverted_at=? WHERE id=?", (now(), change_id)); return True


def dismiss(change_id: int) -> bool:
    """Close one open flag; anything else (applied, dry-run, already closed) is not dismissable."""
    with connection() as conn:
        return bool(conn.execute(f"UPDATE maintenance_changes AS c SET reverted_at=? WHERE c.id=? AND {OPEN_FLAG_SQL}", (now(), change_id)).rowcount)


def attention(conn) -> dict:
    """Badge state: unseen applied changes and unseen open flags, plus a failing-runs marker."""
    try:
        unseen = conn.execute(f"SELECT COUNT(*) FROM maintenance_changes c WHERE c.seen_at IS NULL AND ((c.applied=1 AND c.reverted_at IS NULL) OR ({OPEN_FLAG_SQL}))").fetchone()[0]
    except sqlite3.OperationalError: return {"unseen": 0, "failing_since": None}
    return {"unseen": unseen, "failing_since": maintenance_failing_since(conn)}
