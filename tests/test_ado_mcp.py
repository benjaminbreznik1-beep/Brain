"""Offline tests for the MCP-to-local-panel ADO boundary."""
from __future__ import annotations

import json
import numpy as np
from pathlib import Path
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler

import pytest

from devmem import ado, mcp_server
from devmem.db import EMBEDDING_DIM, EMBEDDING_MODEL, connect, embedding_hash
from devmem.embed import set_embedder


class _Response:
    status = 200

    def __init__(self, body: bytes = b"{}"):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _PanelOpener:
    def __init__(self, body: bytes):
        self.body = body
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, request.get_method(), timeout))
        return _Response(self.body)


def _snapshot():
    raw_prs = json.loads((Path(__file__).parent / "fixtures" / "ado_prs.json").read_text())
    classified = ado.AdoClient(lambda: "test-token", lambda: (("example-org", "ExampleProject"), None))._classify_prs(raw_prs, "me-id", "example-org", "ExampleProject")
    tasks = [
        {"id": 20, "title": "Queued", "type": "Task", "state": "New", "url": "work/20"},
        {"id": 3, "title": "Active", "type": "Bug", "state": "In Progress", "url": "work/3"},
        {"id": 2, "title": "Also active", "type": "Task", "state": "In Progress", "url": "work/2"},
    ]
    return {
        "configured": True, "refreshed_at": "2026-10-27T10:00:00Z", "stale": False,
        "error": None, "project_active_count": 4, **classified, "open_tasks": tasks,
        "open_tasks_by_state": {"New": [tasks[0]], "In Progress": tasks[1:]},
    }


def test_status_shapes_fixture_snapshot_and_filters_sections():
    all_status = mcp_server._ado_status(_snapshot(), "all")
    assert all_status["waiting_for_me"]["to_review"][0]["id"] == 1001
    assert all_status["waiting_on_others"][0]["pending_reviewers"] == ["Reviewer C"]
    assert [task["id"] for task in all_status["tasks"]] == [2, 3, 20]
    assert all_status["tasks_by_state"] == {"New": 1, "In Progress": 2}
    assert set(mcp_server._ado_status(_snapshot(), "tasks")) == {
        "available", "refreshed_at", "stale", "error", "project_active_count", "tasks", "tasks_by_state"
    }


def test_refresh_posts_then_gets_only_fixed_panel_urls(monkeypatch):
    opener = _PanelOpener(json.dumps(_snapshot()).encode())
    monkeypatch.setattr(mcp_server, "_PANEL_OPENER", opener)
    monkeypatch.setenv("DEVMEM_PANEL_PORT", "9876")
    assert mcp_server._panel_snapshot(refresh=True)["configured"] is True
    assert opener.calls == [
        ("http://127.0.0.1:9876/ado/refresh", "POST", 20),
        ("http://127.0.0.1:9876/api/ado", "GET", 3),
    ]


def test_panel_opener_rejects_redirects_and_only_fixed_urls_are_used(monkeypatch):
    real_opener = mcp_server._PANEL_OPENER
    opener = _PanelOpener(json.dumps(_snapshot()).encode())
    monkeypatch.setattr(mcp_server, "_PANEL_OPENER", opener)
    monkeypatch.setenv("DEVMEM_PANEL_PORT", "9876")
    mcp_server._panel_snapshot(refresh=True)
    api, refresh = mcp_server._panel_urls()
    assert {url for url, _, _ in opener.calls} <= {api, refresh}
    assert mcp_server._PanelNoRedirect().redirect_request(None, None, 302, "", {}, "x") is None
    assert not any(isinstance(h, HTTPRedirectHandler) and not isinstance(h, mcp_server._PanelNoRedirect)
                   for h in real_opener.handlers)


def test_refresh_redirect_then_get_and_panel_failures(monkeypatch):
    class RedirectThenGet:
        def __init__(self): self.calls = []
        def open(self, request, timeout):
            self.calls.append((request.get_method(), request.full_url))
            if request.get_method() == "POST":
                raise HTTPError(request.full_url, 303, "See Other", {}, BytesIO())
            return _Response(json.dumps(_snapshot()).encode())
    opener = RedirectThenGet(); monkeypatch.setattr(mcp_server, "_PANEL_OPENER", opener)
    assert mcp_server._panel_snapshot(True)["configured"] is True
    assert [method for method, _ in opener.calls] == ["POST", "GET"]
    class ServerError:
        def open(self, request, timeout): raise HTTPError(request.full_url, 500, "oops", {}, BytesIO())
    monkeypatch.setattr(mcp_server, "_PANEL_OPENER", ServerError())
    monkeypatch.setattr(mcp_server, "_call", lambda tool, args, fn, ctx=None: fn())
    assert mcp_server.ado_status() == {"available": False, "reason": "panel returned HTTP 500"}


def test_panel_down_and_not_configured_are_safe_unavailable_results(monkeypatch):
    class _Down:
        def open(self, request, timeout):
            raise URLError(ConnectionRefusedError())

    monkeypatch.setattr(mcp_server, "_PANEL_OPENER", _Down())
    monkeypatch.setattr(mcp_server, "_call", lambda tool, args, fn, ctx=None: fn())
    down = mcp_server.ado_status()
    assert down == {"available": False, "reason": "devmem panel not running on 127.0.0.1:8765"}
    assert mcp_server._ado_status({"configured": False}, "all") == {
        "available": False, "reason": "ADO not configured (missing token or organization/project; see README 'Azure DevOps')"
    }
    assert mcp_server._ado_status({"configured": False, "config_error": "bad json"}, "all") == {
        "available": False, "reason": "ADO config error: bad json"
    }
    assert mcp_server._ado_status({"configured": False, "missing": ["token", "config"]}, "all") == {
        "available": False, "reason": "ADO not configured (missing token and organization/project)"
    }


def test_panel_defaults_section_validation_and_timeout_note(monkeypatch):
    monkeypatch.delenv("DEVMEM_PANEL_PORT", raising=False)
    assert mcp_server._panel_urls() == ("http://127.0.0.1:8765/api/ado", "http://127.0.0.1:8765/ado/refresh")
    for bad in ("0", "70000", "abc"):
        monkeypatch.setenv("DEVMEM_PANEL_PORT", bad)
        assert ":8765/" in mcp_server._panel_urls()[0]
    for section in ("all", "tasks", "waiting_for_me", "waiting_on_others"):
        assert mcp_server._ado_status(_snapshot(), section)["available"] is True
    monkeypatch.setattr(mcp_server, "_call", lambda tool, args, fn, ctx=None: fn())
    with pytest.raises(ValueError):
        mcp_server.ado_status(section="bad")
    assert mcp_server._ado_status({"pending": True}, "all") == {"available": False, "reason": "ADO data not loaded yet (first refresh pending)"}
    class TimeoutThenCached:
        def open(self, request, timeout):
            if request.get_method() == "POST": raise URLError(TimeoutError())
            return _Response(json.dumps(_snapshot()).encode())
    monkeypatch.setattr(mcp_server, "_PANEL_OPENER", TimeoutThenCached())
    data = mcp_server._panel_snapshot(True)
    assert data.pop("_refresh_timed_out") is True


def test_memory_search_scopes_use_real_scope_sql_with_semantic_results(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    project = str(tmp_path / "project")
    monkeypatch.setenv("DEVMEM_DB", str(db_file))
    conn = connect(db_file)
    ids = {}
    for label, selected_project in (("project", project), ("global", None), ("other", "/other")):
        memory_id = conn.execute("INSERT INTO memories(project,kind,title,content,tags,created_at,updated_at) VALUES(?, 'note', ?, 'unrelated', '', 'x', 'x')",
                                 (selected_project, label)).lastrowid
        vector = np.zeros(EMBEDDING_DIM, dtype=np.float32); vector[0] = 1
        conn.execute("INSERT INTO memory_embeddings(memory_id,model,dim,content_hash,vec) VALUES(?,?,?,?,?)",
                     (memory_id, EMBEDDING_MODEL, EMBEDDING_DIM, embedding_hash(label, "unrelated", ""), vector.tobytes()))
        ids[label] = memory_id
    conn.commit(); conn.close()

    class Embedder:
        def embed(self, texts, *, query=False):
            values = np.zeros((len(texts), EMBEDDING_DIM), dtype=np.float32); values[:, 0] = 1
            return values

    monkeypatch.delenv("DEVMEM_EMBED", raising=False)
    monkeypatch.setattr(mcp_server, "_call", lambda tool, args, fn, ctx=None: fn())
    monkeypatch.setattr(mcp_server, "_client", lambda ctx=None: (1, project))
    set_embedder(Embedder())
    try:
        assert [row["id"] for row in mcp_server.memory_search("needle", scope="project")] == [ids["project"]]
        assert [row["id"] for row in mcp_server.memory_search("needle", scope="global")] == [ids["global"]]
        assert {row["id"] for row in mcp_server.memory_search("needle", scope="all")} == set(ids.values())
    finally:
        set_embedder(None)


def test_ado_status_reports_token_and_refresh_errors():
    token = mcp_server._ado_status({"configured": False, "token_error": "ado_token: not UTF-8 text", "missing": []}, "all")
    assert token["available"] is False and "ADO token error: ado_token: not UTF-8 text" in token["reason"]
    failed = mcp_server._ado_status({"configured": False, "error": "HTTP 404 — check organization/project in the ADO config", "missing": []}, "all")
    assert "ADO error: HTTP 404" in failed["reason"]
