"""Project-path helpers that are safe for hooks to import."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1024)
def project_for_cwd(cwd: str | None) -> str | None:
    """Return git root for cwd, without invoking git; walk for a .git marker."""
    if not cwd:
        return None
    current = Path(cwd).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return str(current)
