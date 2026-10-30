"""Small hook-safe maintenance logger."""
from __future__ import annotations

import os
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
