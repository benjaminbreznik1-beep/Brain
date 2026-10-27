"""Offline coverage for the ADO boundary: no test ever contacts Azure DevOps."""
from __future__ import annotations

import json
from pathlib import Path
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler
import pytest
from fastapi.testclient import TestClient

from devmem import ado
from devmem.ado import AdoClient, AdoError, AdoForbidden, empty_snapshot


class _Response:
    def __init__(self, body=b"{}"):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Opener:
    def __init__(self, body=b"{}"):
        self.body = body
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return _Response(self.body)


class _FailingOpener:
    def __init__(self, token, successful=False):
        self.token, self.successful = token, successful

    def open(self, request, timeout):
        if self.successful:
            return _Response(b'{"authenticatedUser":{"id":"me"}}')
        raise HTTPError(request.full_url, 500, "failure " + self.token, {}, BytesIO(("body " + self.token).encode()))


def test_allowlist_is_checked_before_the_opener(monkeypatch):
    opener = _Opener()
    monkeypatch.setattr(ado, "_OPENER", opener)
    client = AdoClient(lambda: "offline-test-token")

    allowed = [
        ("GET", "https://dev.azure.com/example-org/_apis/connectionData?api-version=7.1", None),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/workitems?ids=1&api-version=7.1", None),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/git/pullrequests?api-version=7.1", None),
        ("POST", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql?api-version=7.1", json.dumps({"query": ado.WIQL}).encode()),
    ]
    for method, url, body in allowed:
        assert client._request(method, url, body) == {}
    assert len(opener.calls) == len(allowed)

    refused = [
        ("PUT", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/workitems", b"{}"),
        ("PATCH", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/workitems", b"{}"),
        ("DELETE", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/workitems", None),
        ("POST", "https://dev.azure.com/example-org/ExampleProject/_apis/git/pullrequests", b"{}"),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/git/repositories", None),
        ("GET", "https://example.test/example-org/_apis/connectionData", None),
        ("GET", "http://dev.azure.com/example-org/_apis/connectionData", None),
        ("GET", "https://dev.azure.com/other/AI/_apis/git/pullrequests", None),
        ("GET", "https://token@dev.azure.com/example-org/_apis/connectionData", None),
        ("GET", "https://dev.azure.com:443/example-org/_apis/connectionData", None),
        ("GET", "https://DEV.AZURE.COM/example-org/_apis/connectionData", None),
        ("GET", "https://dev.azure.com/example-org/_apis/connectionData", b"body"),
        ("POST", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql", None),
        ("POST", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql", b"not-json"),
        ("POST", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql", json.dumps({"query": "SELECT 1"}).encode()),
        ("post", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql", b"not-json"),
    ]
    for method, url, body in refused:
        with pytest.raises(AdoForbidden):
            client._request(method, url, body)
    assert len(opener.calls) == len(allowed)


def test_pr_classification_uses_direct_identity_matches():
    values = json.loads((Path(__file__).parent / "fixtures" / "ado_prs.json").read_text())
    buckets = AdoClient._classify_prs(values, "me-id")
    assert [pr["id"] for pr in buckets["to_review"]] == [101]
    assert [pr["id"] for pr in buckets["my_changes_requested"]] == [102]
    assert [pr["id"] for pr in buckets["my_approved_not_completed"]] == [16054]
    assert [pr["id"] for pr in buckets["my_waiting_on_others"]] == [104]
    assert buckets["my_waiting_on_others"][0]["pending_reviewers"] == ["Reviewer C"]


def test_pr_classification_boundaries_and_groups_are_identity_based():
    prs = [
        {"pullRequestId": 1, "createdBy": {"id": "other"}, "reviewers": [{"id": "not-me", "displayName": "Me", "vote": 0}]},
        {"pullRequestId": 2, "createdBy": {"id": "other"}, "reviewers": [{"id": "team", "displayName": "Team", "vote": 0, "isContainer": True}]},
        {"pullRequestId": 3, "createdBy": {"id": "me-id"}, "reviewers": [{"id": "a", "vote": -10}, {"id": "b", "vote": 10}]},
        {"pullRequestId": 4, "createdBy": {"id": "me-id"}, "reviewers": [{"id": "a", "vote": 5}]},
        {"pullRequestId": 5, "createdBy": {"id": "me-id"}, "reviewers": [{"id": "a", "vote": -5}]},
        {"pullRequestId": 6, "createdBy": {"id": "other"}, "reviewers": [{"id": "me-id", "vote": 10}]},
        {"pullRequestId": 7, "createdBy": {"id": "other"}, "reviewers": []},
    ]
    buckets = AdoClient._classify_prs(prs, "me-id")
    assert [p["id"] for p in buckets["to_review"]] == []
    assert [p["id"] for p in buckets["my_changes_requested"]] == [3, 5]
    assert [p["id"] for p in buckets["my_approved_not_completed"]] == [4]
    assert all(p["id"] != 6 for group in buckets.values() for p in group)
    assert len(prs) == 7  # uninvolved PRs remain in the project count upstream


def test_snapshot_requests_exact_routes_headers_and_buckets(monkeypatch):
    calls = []
    token = "t"
    basic = "Basic " + __import__("base64").b64encode(b":t").decode()
    fixtures = json.loads((Path(__file__).parent / "fixtures" / "ado_prs.json").read_text())
    def body(request):
        calls.append((request.get_method(), request.full_url, request.data, request.headers.get("Authorization")))
        if "connectionData" in request.full_url:
            return {"authenticatedUser": {"id": "me-id", "displayName": "Me"}}
        if "/wiql?" in request.full_url:
            return {"workItems": [{"id": 9}]}
        if "/workitems?" in request.full_url:
            return {"value": [{"id": 9, "fields": {"System.Title": "Task", "System.State": "New", "System.WorkItemType": "Task"}}]}
        return {"value": fixtures}
    class Routes:
        def open(self, request, timeout): return _Response(json.dumps(body(request)).encode())
    monkeypatch.setattr(ado, "_OPENER", Routes())
    snapshot = AdoClient(lambda: token).snapshot()
    assert [(m, u) for m, u, _, _ in calls] == [
        ("GET", "https://dev.azure.com/example-org/_apis/connectionData?api-version=7.1-preview"),
        ("POST", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql?api-version=7.1"),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/workitems?ids=9&fields=System.Id%2CSystem.Title%2CSystem.State%2CSystem.WorkItemType%2CSystem.ChangedDate&api-version=7.1"),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/git/pullrequests?searchCriteria.status=active&%24top=500&api-version=7.1"),
    ]
    assert json.loads(calls[1][2]) == {"query": ado.WIQL}
    assert all(header == basic for _, _, _, header in calls)
    assert snapshot["project_active_count"] == 4
    assert [p["id"] for p in snapshot["to_review"]] == [101]
    assert snapshot["open_tasks_by_state"] == {"New": [snapshot["open_tasks"][0]]}


def test_ado_opener_rejects_redirects():
    assert ado._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test") is None
    assert not any(isinstance(handler, HTTPRedirectHandler) and not isinstance(handler, ado._NoRedirect)
                   for handler in ado._OPENER.handlers)


def test_token_is_absent_from_snapshot_api_html_and_safe_error(monkeypatch, tmp_path):
    from devmem.panel import app as panel

    token = "DISTINCTIVE-ADO-TOKEN-DO-NOT-LEAK"
    monkeypatch.setattr(ado, "_OPENER", _FailingOpener(token))
    cache = panel.AdoCache(client=AdoClient(lambda: token))
    snapshot = cache.refresh()
    assert token not in repr(snapshot)
    assert token not in (snapshot["error"] or "")
    monkeypatch.setattr(panel, "_ado_cache", cache)
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "panel.db"))
    test_client = TestClient(panel.app, base_url="http://127.0.0.1")
    assert token not in test_client.get("/api/ado").text
    assert token not in test_client.get("/ado").text
    assert token not in str(snapshot["error"])
    monkeypatch.setattr(ado, "_OPENER", _FailingOpener(token, successful=True))
    assert token not in repr(panel.AdoCache(client=AdoClient(lambda: token)).refresh())


def test_ado_cache_preserves_last_good_value_and_pulses_only_later_additions():
    from devmem.panel.app import AdoCache
    class Client:
        configured = lambda self: True
        values = [{**empty_snapshot(True), "to_review": [{"id": 1}]}, AdoError("bad"),
                  {**empty_snapshot(True), "to_review": [{"id": 1}, {"id": 2}]}]
        def snapshot(self):
            value = self.values.pop(0)
            if isinstance(value, Exception): raise value
            return value
    pulses = []; cache = AdoCache(Client(), pulses.append)
    assert cache.get()["pending"] is True
    assert cache.refresh()["stale"] is False and pulses == []
    failed = cache.refresh()
    assert failed["stale"] is True and failed["error"] == "bad" and failed["to_review"] == [{"id": 1}]
    assert cache.refresh()["to_review"][-1]["id"] == 2
    assert pulses == [{"type": "ado", "id": 2}]


def test_ado_changes_ignore_initial_and_identical_snapshots():
    from devmem.panel.app import AdoCache
    snapshot = {**empty_snapshot(True), "open_tasks": [{"id": 1, "title": "Task", "state": "New"}]}
    class Client:
        configured = lambda self: True
        def snapshot(self): return snapshot
    cache = AdoCache(Client())
    cache.refresh()
    cache.refresh()
    assert cache.get_changes() == (0, [])


def test_ado_changes_describe_task_additions_states_and_removals():
    from devmem.panel.app import AdoCache
    first = {**empty_snapshot(True), "open_tasks": [{"id": 1, "title": "Old", "state": "New"},
             {"id": 2, "title": "Gone", "state": "Active"}]}
    second = {**empty_snapshot(True), "open_tasks": [{"id": 1, "title": "Old", "state": "Closed"},
              {"id": 3, "title": "New", "state": "Active"}]}
    class Client:
        configured = lambda self: True
        values = [first, second]
        def snapshot(self): return self.values.pop(0)
    cache = AdoCache(Client())
    cache.refresh(); cache.refresh()
    assert [change["text"] for change in reversed(cache.get_changes()[1])] == [
        "Task #1 Old: New → Closed", "Task #3 New: new, Active", "Task #2 Gone: no longer open"]


def test_ado_changes_describe_pr_lists_and_pending_reviewers():
    from devmem.panel.app import AdoCache
    first = {**empty_snapshot(True), "to_review": [{"id": 1, "title": "Review"}],
             "my_waiting_on_others": [{"id": 2, "title": "Wait", "pending_reviewers": ["Ada"]}],
             "my_changes_requested": [{"id": 3, "title": "Gone"}]}
    second = {**empty_snapshot(True), "to_review": [{"id": 4, "title": "New review"}],
              "my_approved_not_completed": [{"id": 1, "title": "Review"}],
              "my_waiting_on_others": [{"id": 2, "title": "Wait", "pending_reviewers": ["Bea"]}]}
    class Client:
        configured = lambda self: True
        values = [first, second]
        def snapshot(self): return self.values.pop(0)
    cache = AdoCache(Client())
    cache.refresh(); cache.refresh()
    assert [change["text"] for change in reversed(cache.get_changes()[1])] == [
        "PR !4 New review: now in to review", "PR !1 Review: now in approved, not completed", "PR !2 Wait: pending reviewers now Bea",
        "PR !3 Gone: no longer listed"]


def test_ado_changes_keep_earlier_records_after_a_failed_refresh():
    from devmem.panel.app import AdoCache
    first = {**empty_snapshot(True), "open_tasks": []}
    second = {**empty_snapshot(True), "open_tasks": [{"id": 1, "title": "Task", "state": "New"}]}
    class Client:
        configured = lambda self: True
        values = [first, second, AdoError("bad")]
        def snapshot(self):
            value = self.values.pop(0)
            if isinstance(value, Exception): raise value
            return value
    cache = AdoCache(Client())
    cache.refresh(); cache.refresh()
    before = cache.get_changes()
    cache.refresh()
    assert cache.get_changes() == before


def test_ado_changes_are_capped_and_keep_increasing_sequence_numbers():
    from devmem.panel.app import AdoCache
    values = [{**empty_snapshot(True), "open_tasks": [{"id": 1, "title": "Task", "state": "0"}]}]
    values.extend({**empty_snapshot(True), "open_tasks": [{"id": 1, "title": "Task", "state": str(number)}]}
                  for number in range(1, 106))
    class Client:
        configured = lambda self: True
        def snapshot(self): return values.pop(0)
    cache = AdoCache(Client())
    for _ in range(106): cache.refresh()
    latest, changes = cache.get_changes()
    assert latest == 105 and len(changes) == 100
    assert changes[0]["seq"] == 105 and changes[-1]["seq"] == 6


def test_ado_changes_api_reads_only_the_cache_and_filters_since(tmp_path, monkeypatch):
    from devmem.panel import app as panel
    class Client:
        def __init__(self): self.calls = 0
        def configured(self): return True
        def snapshot(self): self.calls += 1; return empty_snapshot(True)
    source = Client(); cache = panel.AdoCache(source)
    cache.change_seq = 2
    cache.changes = [{"seq": 1, "at": "2026-01-01T00:00:00Z", "text": "first"},
                     {"seq": 2, "at": "2026-01-01T00:01:00Z", "text": "second"}]
    monkeypatch.setattr(panel, "_ado_cache", cache)
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "panel.db"))
    client = TestClient(panel.app, base_url="http://127.0.0.1")
    assert client.get("/api/ado/changes").json() == {"journal_id": cache.journal_id, "latest_seq": 2, "changes": list(reversed(cache.changes))}
    assert client.get("/api/ado/changes?since=1").json() == {"journal_id": cache.journal_id, "latest_seq": 2, "changes": [cache.changes[1]]}
    cache.changes = []; cache.change_seq = 0
    assert client.get("/api/ado/changes").json() == {"journal_id": cache.journal_id, "latest_seq": 0, "changes": []}
    assert source.calls == 0
    assert panel.AdoCache(source).journal_id != cache.journal_id  # a new panel run gets a new journal


def test_pages_include_the_ado_nav_link_and_changes_container(tmp_path, monkeypatch):
    from devmem.panel import app as panel
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "panel.db"))
    class Client:
        def configured(self): return False
    cache = panel.AdoCache(Client())
    monkeypatch.setattr(panel, "_ado_cache", cache)
    client = TestClient(panel.app, base_url="http://127.0.0.1")
    overview, ado_page = client.get("/overview").text, client.get("/ado").text
    assert 'href="/ado"' in overview and 'href="/ado"' in ado_page
    assert 'id="ado-changes"' in ado_page


def test_not_configured_makes_zero_requests(monkeypatch):
    opener = _Opener()
    monkeypatch.setattr(ado, "_OPENER", opener)
    snapshot = AdoClient(lambda: None).snapshot()
    assert snapshot == empty_snapshot()
    assert opener.calls == []


def test_panel_ado_routes_render_cached_states_and_reject_untrusted_hosts(tmp_path, monkeypatch):
    """Exercise public routes without entering the lifespan workers."""
    from devmem.panel import app as panel
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "panel.db"))

    class Client:
        def __init__(self): self.calls = 0
        def configured(self): return True
        def snapshot(self):
            self.calls += 1
            return {**empty_snapshot(True), "to_review": [{"id": 7, "title": "Review me", "repo": "AI",
                "url": "https://example.test/7", "created_date": "now", "reviewers": [],
                "pending_reviewers": [], "age_label": "1h old"}], "stale": True, "error": "cached ADO failure"}

    source = Client()
    cache = panel.AdoCache(source)
    monkeypatch.setattr(panel, "_ado_cache", cache)
    client = TestClient(panel.app, base_url="http://127.0.0.1")
    response = client.post("/ado/refresh", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/ado"
    assert source.calls == 1
    page = client.get("/ado")
    assert page.status_code == 200
    assert "Review me" in page.text and "stale" in page.text and "cached ADO failure" in page.text
    assert client.get("/api/ado").json() == cache.get()
    cache.value = empty_snapshot(True)
    assert client.get("/ado").status_code == 200
    cache.value = {**empty_snapshot(True), "pending": True}
    assert client.get("/ado").status_code == 200
    assert client.get("/ado", headers={"Host": "untrusted.example"}).status_code == 400
