"""Keep the optional embedding runtime out of the offline test suite."""
import os
import subprocess

import pytest

from devmem import ado, maintenance, mcp_server

_REAL_RUN, _REAL_ENGINE_RUN = subprocess.run, maintenance.Engine._run


@pytest.fixture(autouse=True)
def _test_db(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "devmem.db"))
    monkeypatch.setenv("DEVMEM_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("DEVMEM_AUDIT", str(tmp_path / "audit.json"))
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(tmp_path / "ado.json"))
    monkeypatch.setenv("DEVMEM_GIT_SYNC", str(tmp_path / "git-sync.json"))
    monkeypatch.setenv("DEVMEM_GIT_SYNC_STATE", str(tmp_path / "git-sync-state.json"))
    monkeypatch.setenv("DEVMEM_GIT_SYNC_KEY", str(tmp_path / "git-sync-key"))
    monkeypatch.setenv("DEVMEM_GIT_DIR", str(tmp_path / "git-memories"))
    monkeypatch.setenv("DEVMEM_GIT_SYNC_LOCK", str(tmp_path / "git-sync.lock"))
    monkeypatch.setenv("DEVMEM_DISPATCH", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("DEVMEM_DISPATCH_LOCK", str(tmp_path / "dispatch.lock"))
    # Maintenance must never see the real ~/.devmem lock, config or log.
    monkeypatch.setenv("DEVMEM_MAINTAIN", str(tmp_path / "maintain.json"))
    monkeypatch.setenv("DEVMEM_MAINTAIN_LOCK", str(tmp_path / "maintain.lock"))
    monkeypatch.setenv("DEVMEM_MAINTAIN_LOG", str(tmp_path / "maintain.log"))
    monkeypatch.setenv("DEVMEM_GUARD_LOG", str(tmp_path / "guard.log"))
    monkeypatch.setenv("DEVMEM_GUARD_HOOK_ERR", str(tmp_path / "guard-hook.err"))
    # The log scanner must never read the real ~/.claude, ~/.codex or status-line snapshot.
    monkeypatch.setenv("DEVMEM_CLAUDE_HOME", str(tmp_path / "claude_home"))
    monkeypatch.setenv("DEVMEM_CODEX_HOME", str(tmp_path / "codex_home"))
    monkeypatch.setenv("DEVMEM_CLAUDE_LIMITS", str(tmp_path / "claude_limits.json"))
    monkeypatch.setenv("DEVMEM_REMINDERS", str(tmp_path / "reminders.json"))
    monkeypatch.delenv("DEVMEM_MAINTAIN_CHILD", raising=False)
    monkeypatch.delenv("DEVMEM_ADO_ORG", raising=False)
    monkeypatch.delenv("DEVMEM_ADO_PROJECT", raising=False)
    monkeypatch.setattr(ado, "TOKEN_PATH", tmp_path / "ado_token")
    monkeypatch.setattr(ado, "CONFIG_PATH", tmp_path / "ado.json")
    # Block I/O but keep the real openers, so tests can still inspect their redirect handlers.
    def no_network(*args, **kwargs):
        raise AssertionError("test attempted network I/O")
    monkeypatch.setattr(ado._OPENER, "open", no_network)
    monkeypatch.setattr(mcp_server._PANEL_OPENER, "open", no_network)


_REAL_POPEN = subprocess.Popen


class _GuardedPopen(_REAL_POPEN):
    """Every subprocess (subprocess.run included) goes through Popen: refuse the real codex/claude CLIs (quota, writes to ~/.codex)."""
    def __init__(self, args, *rest, **kwargs):
        first = args if isinstance(args, (str, bytes, os.PathLike)) else (list(args)[:1] or [""])[0]
        name = os.path.basename(os.fsdecode(first).split()[0] if isinstance(args, (str, bytes)) and first else os.fsdecode(first))
        if name in ("codex", "claude"): raise AssertionError(f"test attempted to launch the real {name} CLI")
        super().__init__(args, *rest, **kwargs)


@pytest.fixture(autouse=True)
def _no_real_engines(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
    def refuse(self, *args, **kwargs):  # tests that exercise Engine._run replace subprocess.run with a fake first
        if subprocess.run is _REAL_RUN: raise AssertionError("test attempted to run the real maintenance engine")
        return _REAL_ENGINE_RUN(self, *args, **kwargs)
    monkeypatch.setattr(maintenance.Engine, "_run", refuse)


@pytest.fixture(autouse=True)
def _disable_embeddings(monkeypatch, _test_db):
    monkeypatch.setenv("DEVMEM_EMBED", "0")
