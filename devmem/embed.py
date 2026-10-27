"""Lazy, best-effort local embeddings for hybrid memory search."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384
IDLE_UNLOAD_S = 10 * 60
FAILURE_BACKOFF_S = 10 * 60


class Embedder(Protocol):
    """Small interface deliberately easy to replace in offline tests."""

    def embed(self, texts: Sequence[str], *, query: bool = False) -> np.ndarray | None: ...


class FastEmbedder:
    """Load FastEmbed only at first use and release it after a quiet period."""

    def __init__(self, model: str = EMBEDDING_MODEL, cache_dir: Path | None = None):
        self.model = model
        self.cache_dir = cache_dir or Path("~/.devmem/models").expanduser()
        self._lock = threading.RLock()
        self._model = None
        self._last_use = 0.0
        self._disabled_until = 0.0
        self._unload_timer: threading.Timer | None = None

    def _fail(self, exc: Exception) -> None:
        if self._unload_timer is not None:
            self._unload_timer.cancel()
            self._unload_timer = None
        self._model = None
        self._disabled_until = time.monotonic() + FAILURE_BACKOFF_S
        print(f"devmem: embeddings unavailable for 10 minutes ({type(exc).__name__})", file=sys.stderr)

    def _schedule_unload(self) -> None:
        if self._unload_timer is not None:
            self._unload_timer.cancel()

        def unload() -> None:
            with self._lock:
                if time.monotonic() - self._last_use >= IDLE_UNLOAD_S:
                    self._model = None
                    self._unload_timer = None

        self._unload_timer = threading.Timer(IDLE_UNLOAD_S, unload)
        self._unload_timer.daemon = True
        self._unload_timer.start()

    def embed(self, texts: Sequence[str], *, query: bool = False) -> np.ndarray | None:
        if os.environ.get("DEVMEM_EMBED") == "0" or not texts:
            return None
        with self._lock:
            if time.monotonic() < self._disabled_until:
                return None
            try:
                if self._model is not None and time.monotonic() - self._last_use >= IDLE_UNLOAD_S:
                    self._model = None
                if self._model is None:
                    # Importing fastembed itself can fail when its optional runtime is unavailable.
                    from fastembed import TextEmbedding
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    self._model = TextEmbedding(self.model, cache_dir=str(self.cache_dir))
                method = self._model.query_embed if query and hasattr(self._model, "query_embed") else self._model.embed
                vectors = np.asarray(list(method(list(texts))), dtype=np.float32)
                if vectors.ndim != 2 or vectors.shape != (len(texts), EMBEDDING_DIM):
                    raise ValueError("embedding model returned unexpected vector shape")
                self._last_use = time.monotonic()
                self._schedule_unload()
                return vectors
            except Exception as exc:  # Embeddings are an optional local acceleration, never a request failure.
                self._fail(exc)
                return None


_embedder: Embedder | None = None
_embedder_lock = threading.Lock()


def get_embedder() -> Embedder:
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            _embedder = FastEmbedder()
        return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    """Replace the process embedder (primarily for deterministic offline tests)."""
    global _embedder
    with _embedder_lock:
        _embedder = embedder
