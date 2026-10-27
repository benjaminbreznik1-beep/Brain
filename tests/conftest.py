"""Keep the optional embedding runtime out of the offline test suite."""
import pytest


@pytest.fixture(autouse=True)
def _test_db(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "devmem.db"))


@pytest.fixture(autouse=True)
def _disable_embeddings(monkeypatch, _test_db):
    monkeypatch.setenv("DEVMEM_EMBED", "0")
