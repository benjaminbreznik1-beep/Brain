"""Small hook-safe maintenance logger."""
from __future__ import annotations

import os
import json
from datetime import UTC, datetime
from pathlib import Path


def _maintain_log() -> Path:
    return Path(os.environ.get("DEVMEM_MAINTAIN_LOG", "~/.devmem/maintain.log")).expanduser()


def _open_maintain_log():
    """Append handle for detached maintenance output; rotated at 1 MB so it cannot grow without bound."""
    path = _maintain_log(); path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 1_000_000: path.replace(path.with_name(path.name + ".1"))
    return open(path, "ab")


def _log(message: str) -> None:
    try:
        with _open_maintain_log() as log: log.write(f"{datetime.now(UTC).isoformat()} {message}\n".encode())
    except OSError:
        pass


def _guard_log() -> Path:
    return Path(os.environ.get("DEVMEM_GUARD_LOG", "~/.devmem/guard.log")).expanduser()


def guard_log(message: str, *, db_path: str, tool_name: str, cwd: str | None) -> None:
    """Append a rate-limited guard failure without affecting the hook."""
    try:
        path = _guard_log(); state_path = path.with_name(path.name + ".state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        try: state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception: state = {}
        stamp = datetime.now(UTC).timestamp()
        if stamp - float(state.get(message, 0)) < 600: return
        state[message] = stamp
        state_path.write_text(json.dumps(state), encoding="utf-8")
        try:
            if path.is_file() and path.stat().st_size > 1_000_000: path.replace(path.with_name(path.name + ".1"))
        except FileNotFoundError: pass
        with open(path, "ab") as log:
            log.write(f"{datetime.now(UTC).isoformat()} db={db_path} tool={tool_name} cwd={cwd or ''} {message}\n".encode(errors="replace"))
    except Exception:
        pass
