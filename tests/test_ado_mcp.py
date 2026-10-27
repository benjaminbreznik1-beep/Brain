"""Offline tests for the MCP-to-local-panel ADO boundary."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from devmem import ado, mcp_server


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
    classified = ado.AdoClient._classify_prs(raw_prs, "me-id")
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
    assert all_status["waiting_for_me"]["to_review"][0]["id"] == 101
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


def test_panel_down_and_not_configured_are_safe_unavailable_results(monkeypatch):
    class _Down:
        def open(self, request, timeout):
            raise URLError("offline")

    monkeypatch.setattr(mcp_server, "_PANEL_OPENER", _Down())
    monkeypatch.setattr(mcp_server, "_call", lambda tool, args, fn, ctx=None: fn())
    down = mcp_server.ado_status()
    assert down == {"available": False, "reason": "devmem panel not running on 127.0.0.1:8765"}
    assert mcp_server._ado_status({"configured": False}, "all") == {
        "available": False, "reason": "ADO not configured (no token)"
    }


def test_no_unapproved_url_can_be_requested(monkeypatch):
    requested = []

    class _Guard:
        def open(self, request, timeout):
            requested.append(request.full_url)
            raise AssertionError("network request attempted")

    monkeypatch.setattr(mcp_server, "_PANEL_OPENER", _Guard())
    monkeypatch.setenv("DEVMEM_PANEL_PORT", "not-a-port")
    with pytest.raises(AssertionError):
        mcp_server._panel_snapshot(refresh=False)
    assert requested == ["http://127.0.0.1:8765/api/ado"]
