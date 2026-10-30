"""Keep the optional embedding runtime out of the offline test suite."""
import pytest

from devmem import ado, mcp_server


@pytest.fixture(autouse=True)
def _test_db(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "devmem.db"))
    monkeypatch.setenv("DEVMEM_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("DEVMEM_AUDIT", str(tmp_path / "audit.json"))
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(tmp_path / "ado.json"))
    monkeypatch.delenv("DEVMEM_ADO_ORG", raising=False)
    monkeypatch.delenv("DEVMEM_ADO_PROJECT", raising=False)
    monkeypatch.setattr(ado, "TOKEN_PATH", tmp_path / "ado_token")
    monkeypatch.setattr(ado, "CONFIG_PATH", tmp_path / "ado.json")
    # Block I/O but keep the real openers, so tests can still inspect their redirect handlers.
    def no_network(*args, **kwargs):
        raise AssertionError("test attempted network I/O")
    monkeypatch.setattr(ado._OPENER, "open", no_network)
    monkeypatch.setattr(mcp_server._PANEL_OPENER, "open", no_network)


@pytest.fixture(autouse=True)
def _disable_embeddings(monkeypatch, _test_db):
    monkeypatch.setenv("DEVMEM_EMBED", "0")
