"""Keep the optional embedding runtime out of the offline test suite."""
import pytest


@pytest.fixture(autouse=True)
def _disable_embeddings(monkeypatch):
    monkeypatch.setenv("DEVMEM_EMBED", "0")
