from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import subprocess
from pathlib import Path

from .db import audit_memories, context_text, connection, db_path, ensure_embeddings, fts_query, hybrid_search, now, project_for_cwd, readonly_connection
from .embed import get_embedder


_NO_CONTEXT = "No saved project context."
_HOOK_HEADER = "devmem memory (auto-loaded at session start; use memory_search / memory_get for more):"
_REMINDER_RULES = (
    (r"\bgit\s+(switch\s+-c|checkout\s+-b)\b", "New branch started: if this belongs to a tracked task, update its status line in memory (the pinned board and the task's own memory)."),
    (r"\bgit\s+commit\b", "Commit made: update the task's status in memory (pinned board + task memory) if its phase changed."),
    (r"\bgit\s+merge\b", "Merge done: mark the task's phase in memory (pinned board + task memory)."),
    (r"\bcodex\s+exec\b", "Work dispatched to Codex: record 'dispatched' in the task's memory now and update it again when the result comes back."),
)


def _hook_cwd() -> str:
    try:
        payload = json.load(sys.stdin)
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        return cwd if isinstance(cwd, str) else os.getcwd()
    except Exception:
        return os.getcwd()


def _maintain_log() -> Path:
    return Path(os.environ.get("DEVMEM_MAINTAIN_LOG", "~/.devmem/maintain.log")).expanduser()


def _open_maintain_log():
    """Append handle for detached maintenance output; rotated at 1 MB so it cannot grow without bound."""
    path = _maintain_log(); path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 1_000_000: path.replace(path.with_name(path.name + ".1"))
    return open(path, "ab")


def _log(message: str) -> None:
    try:
        with _open_maintain_log() as log: log.write(f"{now()} {message}\n".encode())
    except OSError:
        pass


def _spawn_maintain(*args: str) -> None:
    """Start a detached maintenance run that never shares the hook's stdin and logs its output and errors."""
    try: log = _open_maintain_log()
    except OSError: log = subprocess.DEVNULL
    try:
        subprocess.Popen([sys.executable, "-m", "devmem.cli", "maintain", *args], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    finally:
        if log is not subprocess.DEVNULL: log.close()


def _start_maintenance_if_due() -> None:
    try:
        from .maintenance import due_for_startup
        if due_for_startup(): _spawn_maintain("--startup")
    except Exception as exc:
        _log(f"startup maintenance not started: {type(exc).__name__}: {exc}")


def _maintain_child() -> bool:
    """True inside a maintenance engine (claude -p) session: its hooks must not start more maintenance."""
    from .maintenance import CHILD_ENV
    return bool(os.environ.get(CHILD_ENV))


def _session_end_hook() -> None:
    """A SessionEnd hook must never delay or fail Claude; problems leave a line in the maintenance log."""
    try:
        if _maintain_child(): return
        from .maintenance import config
        cfg = config()
        if not cfg["enabled"] and not cfg.get("error"): return
        try:
            payload = json.load(sys.stdin)
        except Exception as exc:
            payload = {}; _log(f"SessionEnd hook: unreadable hook input ({type(exc).__name__}); using the process cwd")
        payload = payload if isinstance(payload, dict) else {}
        cwd = payload.get("cwd")
        if not isinstance(cwd, str):
            cwd = os.getcwd(); _log("SessionEnd hook: no cwd in hook input; using the process cwd")
        command = ["--session-end", "--project", project_for_cwd(cwd)]
        if isinstance(payload.get("transcript_path"), str): command += ["--transcript", payload["transcript_path"]]
        _spawn_maintain(*command)
    except Exception as exc:
        _log(f"SessionEnd hook failed: {type(exc).__name__}: {exc}")


def _print_proposals(changes: list[dict]) -> None:
    for change in changes:
        target = f" -> #{change['target_id']}" if change.get("target_id") else ""
        state = f"blocked: {change['blocked_by']}" if change.get("blocked_by") else "would apply"
        print(f"#{change['memory_id']}{target} {change['action']} [{state}] {change.get('summary') or ''}")
        if change.get("new_title"): print(f"    title: {change['new_title']}")
        if change.get("new_content"): print("    " + str(change["new_content"]).replace("\n", "\n    "))


def _context_for_cwd(cwd: str) -> str:
    if not db_path().is_file():
        return _NO_CONTEXT
    with readonly_connection() as conn:
        return context_text(conn, project_for_cwd(cwd))


def _reminder_rules():
    path = os.environ.get("DEVMEM_REMINDERS", "~/.devmem/reminders.json")
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _REMINDER_RULES
    except Exception as exc:
        print(f"devmem remind: {exc}", file=sys.stderr)
        return ()
    try:
        if not isinstance(data, dict) or not isinstance(data["rules"], list): raise ValueError("invalid reminders config")
        rules = []
        for rule in data["rules"]:
            if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str) or not isinstance(rule.get("message"), str):
                print("devmem remind: invalid rule", file=sys.stderr); continue
            rules.append((rule["pattern"], rule["message"]))
    except Exception as exc:
        print(f"devmem remind: {exc}", file=sys.stderr)
        return ()
    return rules


def _remind_hook() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("tool_name") != "Bash": return
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str): return
        for pattern, message in _reminder_rules():
            try:
                if re.search(pattern, command):
                    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "devmem reminder: " + message}}, separators=(",", ":")))
                    return
            except re.error as exc:
                print(f"devmem remind: invalid regex: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"devmem remind: {exc}", file=sys.stderr)

def _ask_passphrase() -> str:
    """Interactive only (getpass), never an argument, so it cannot land in shell history or ps."""
    import getpass
    first = getpass.getpass("New passphrase: ")
    if not first: raise SystemExit("devmem remote: the passphrase must not be empty")
    if getpass.getpass("Repeat passphrase: ") != first: raise SystemExit("devmem remote: the passphrases do not match")
    return first


def _remote_command(args) -> None:
    from . import remote
    try: cfg = remote.load_config()
    except ValueError as exc: raise SystemExit(f"devmem remote: {exc}")
    if args.remote_command == "setup":
        try: url = remote.normalise_url(args.url)
        except ValueError as exc: raise SystemExit(f"devmem remote: {exc}")
        had_passphrase = bool(cfg.get("passphrase_scrypt"))
        cfg["passphrase_scrypt"] = remote.hash_passphrase(_ask_passphrase())
        cfg.update(enabled=True, public_url=url)
        cfg.setdefault("redirect_allowlist", list(remote.DEFAULT_REDIRECTS))
        hosts = cfg.get("panel_hosts") if isinstance(cfg.get("panel_hosts"), list) else []
        host = url.removeprefix("https://").split(":", 1)[0]
        cfg["panel_hosts"] = hosts if host in hosts else [*hosts, host]
        remote.save_config(cfg)
        revoked = remote.revoke_all_tokens() if had_passphrase else 0
        print(f"Remote access enabled for {url} (connector URL {url}/mcp); config in {remote.config_path()}."
              + (f" Passphrase replaced: {revoked} tokens revoked." if had_passphrase else ""))
    elif args.remote_command == "passphrase":
        if remote.setup_problem({**cfg, "passphrase_scrypt": "x"}): raise SystemExit("devmem remote: run `devmem remote setup --url ...` first")
        cfg["passphrase_scrypt"] = remote.hash_passphrase(_ask_passphrase())
        remote.save_config(cfg)
        print(f"Passphrase changed; {remote.revoke_all_tokens()} tokens revoked. Clients must approve again.")
    elif args.remote_command == "status":
        print(f"enabled: {bool(cfg.get('enabled'))}  public_url: {cfg.get('public_url') or '-'}  passphrase: {'set' if cfg.get('passphrase_scrypt') else 'not set'}"
              f"  mcp-http: {'running' if remote.mcp_http_running() else 'not running'}")
        rows = remote.clients()
        if not rows: print("No remote clients registered.")
        for c in rows:
            state = "revoked" if c["revoked_at"] else "active"
            print(f"{c['client_id']}\t{c['client_name'] or 'unnamed'}\t{state}\tregistered {c['created_at']}\tlast used {c['last_used_at'] or 'never'}"
                  f"\taccess {c['access_tokens'] or 0}\trefresh {c['refresh_tokens'] or 0}")
    elif args.remote_command == "revoke":
        if args.client_id == "all":
            print(f"Revoked {remote.revoke_all_clients()} clients.")
        elif remote.revoke_client(args.client_id):
            print(f"Revoked {args.client_id}.")
        else:
            raise SystemExit(f"devmem remote: no active client {args.client_id}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="devmem")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp")
    mcp_http = sub.add_parser("mcp-http"); mcp_http.add_argument("--port", type=int, default=8766)
    remote_cmd = sub.add_parser("remote"); remote_sub = remote_cmd.add_subparsers(dest="remote_command", required=True)
    remote_sub.add_parser("setup").add_argument("--url", required=True)
    remote_sub.add_parser("passphrase"); remote_sub.add_parser("status")
    remote_sub.add_parser("revoke").add_argument("client_id")
    sub.add_parser("ingest")
    sub.add_parser("embed")
    sub.add_parser("audit")
    panel = sub.add_parser("panel"); panel.add_argument("--port", type=int, default=8765)
    search = sub.add_parser("search"); search.add_argument("q"); search.add_argument("--limit", type=int, default=8)
    context = sub.add_parser("context")
    context.add_argument("--cwd")
    context.add_argument("--hook", action="store_true")
    remind = sub.add_parser("remind")
    remind.add_argument("--hook", action="store_true")
    maintain = sub.add_parser("maintain")
    maintain.add_argument("--project")
    maintain.add_argument("--dry-run", action="store_true")
    maintain.add_argument("--limit", type=int)
    maintain.add_argument("--session-end", action="store_true")
    maintain.add_argument("--startup", action="store_true")
    maintain.add_argument("--hook", action="store_true")
    maintain.add_argument("--transcript")
    cleanup = sub.add_parser("maintenance-cleanup")
    cleanup.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "mcp":
        from .mcp_server import run; run()
    elif args.command == "mcp-http":
        from .remote import run; run(args.port)
    elif args.command == "remote":
        _remote_command(args)
    elif args.command == "ingest":
        from .ingest.runner import ingest_all; print(f"Ingested {ingest_all()} new usage events.")
    elif args.command == "embed":
        total = 0
        with connection() as conn:
            while True:
                result = ensure_embeddings(conn)
                total += result["embedded"]
                if not result["embedded"]:
                    print(f"Embedded {total} memories; {result['pending']} pending.")
                    if result["pending"]:
                        status = getattr(get_embedder(), "status", lambda: {})()
                        print(f"devmem embed: {status.get('last_error') or 'embedder unavailable'}", file=sys.stderr)
                        raise SystemExit(1)
                    break
    elif args.command == "audit":
        with connection() as conn:
            rows = audit_memories(conn)
        if not rows:
            print("No audit findings.")
        for row in rows:
            print(f"#{row['id']}\t{row['title']}\t{row['size']} chars\t{row['updated_at']}\t{row['reason']}")
    elif args.command == "panel":
        _start_maintenance_if_due()
        import uvicorn
        from .panel.app import _shutdown_event, app

        class PanelServer(uvicorn.Server):
            def handle_exit(self, sig, frame):
                _shutdown_event.set()
                super().handle_exit(sig, frame)

        _shutdown_event.clear()
        config = uvicorn.Config(app, host="127.0.0.1", port=args.port, timeout_graceful_shutdown=2)
        PanelServer(config).run()
    elif args.command == "context":
        try:
            if args.hook and _maintain_child():
                return
            if not db_path().is_file():
                return
            briefing = _context_for_cwd(_hook_cwd() if args.hook else args.cwd or os.getcwd())
            if args.hook and briefing == _NO_CONTEXT:
                return
            if args.hook:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                          "additionalContext": f"{_HOOK_HEADER}\n{briefing}"}}))
                # Deliberately after printing: a SessionStart briefing is never delayed or changed by maintenance.
                sys.stdout.flush()
                _start_maintenance_if_due()
            else:
                print(briefing)
        except Exception as exc:
            print(f"devmem context: {exc}", file=sys.stderr)
    elif args.command == "remind":
        if args.hook: _remind_hook()
    elif args.command == "maintain":
        if args.hook:
            if not args.session_end: parser.error("maintain --hook is only for the SessionEnd hook (--session-end --hook)")
            _session_end_hook(); return
        if args.limit is not None and args.limit < 1: parser.error("--limit must be at least 1")
        from .maintenance import run
        # SIGTERM becomes SystemExit, so the run row is marked as interrupted instead of staying 'running'.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        # Memories store the project's root (project_for_cwd), so a subdirectory must be normalised the same way.
        project = project_for_cwd(args.project) if args.project else project_for_cwd(os.getcwd()) if args.session_end else None
        trigger = "session_end" if args.session_end else "startup" if args.startup else "manual"
        try:
            result = run(trigger, project=project, dry_run=args.dry_run, limit=args.limit,
                         transcript_path=args.transcript if args.session_end else None)
        except BaseException as exc:
            print(f"{now()} devmem maintain failed: {type(exc).__name__}: {exc}"[:500], file=sys.stderr)
            raise SystemExit(1)
        _print_proposals(result.pop("changes", []))
        print(json.dumps(result, separators=(",", ":")))
        if result["status"] == "error": raise SystemExit(1)
    elif args.command == "maintenance-cleanup":
        from .maintenance import cleanup
        result = cleanup(dry_run=args.dry_run)
        print(result.get("line") or result["status"])
    else:
        if not fts_query(args.q): return
        with connection() as conn:
            rows = hybrid_search(conn, args.q, where_sql="1=1", limit=args.limit)
        for r in rows: print(f"{r['id']} [{r['kind']}] {r['title']} — {r['snippet']}")

if __name__ == "__main__": main()
