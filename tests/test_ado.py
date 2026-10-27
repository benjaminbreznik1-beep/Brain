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

CONFIG = ("example-org", "ExampleProject")


def _client(token="offline-test-token", config=CONFIG):
    return AdoClient(lambda: token, lambda: (config, None))


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
    client = _client()

    allowed = [
        ("GET", "https://dev.azure.com/example-org/_apis/connectionData?api-version=7.1", None),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/workitems?ids=1&api-version=7.1", None),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/git/pullrequests?api-version=7.1", None),
        ("POST", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql?api-version=7.1", json.dumps({"query": client._wiql(CONFIG[1])}).encode()),
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
        ("GET", "https://dev.azure.com/other-org/ExampleProject/_apis/git/pullrequests", None),
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


def test_project_spaces_are_quoted_and_old_org_paths_are_forbidden(monkeypatch):
    opener = _Opener()
    monkeypatch.setattr(ado, "_OPENER", opener)
    client = _client(config=("example-org", "Example Project"))
    allowed = "https://dev.azure.com/example-org/Example%20Project/_apis/git/pullrequests"
    assert client._request("GET", allowed) == {}
    for forbidden in (
        "https://dev.azure.com/example-org/Example Project/_apis/git/pullrequests",
        "https://dev.azure.com/old-org/Example%20Project/_apis/git/pullrequests",
    ):
        with pytest.raises(AdoForbidden):
            client._request("GET", forbidden)
    assert len(opener.calls) == 1


def test_invalid_or_missing_config_is_not_configured_and_never_calls_network(monkeypatch):
    opener = _Opener()
    monkeypatch.setattr(ado, "_OPENER", opener)
    for config in (None, ("bad/org", "ExampleProject"), ("example-org", "")):
        client = _client(config=config)
        assert client.configured() is False
        assert client.snapshot()["configured"] is False
    assert opener.calls == []


def test_config_file_values_and_environment_overrides(tmp_path, monkeypatch):
    config_path = tmp_path / "ado.json"
    config_path.write_text('{"organization":"file-org","project":"File Project"}')
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(config_path))
    monkeypatch.delenv("DEVMEM_ADO_ORG", raising=False)
    monkeypatch.delenv("DEVMEM_ADO_PROJECT", raising=False)
    assert ado._read_config() == (("file-org", "File Project"), None)
    monkeypatch.setenv("DEVMEM_ADO_ORG", "environment-org")
    monkeypatch.setenv("DEVMEM_ADO_PROJECT", "EnvironmentProject")
    assert ado._read_config() == (("environment-org", "EnvironmentProject"), None)


def test_wiql_uses_the_configured_project_and_escapes_quotes():
    client = _client(config=("example-org", "ExampleProject"))
    assert "[System.TeamProject] = 'ExampleProject'" in client._wiql(CONFIG[1])
    assert "[System.TeamProject] = 'Example''Project'" in AdoClient._wiql("Example'Project")


def test_pr_classification_uses_direct_identity_matches():
    values = json.loads((Path(__file__).parent / "fixtures" / "ado_prs.json").read_text())
    buckets = _client()._classify_prs(values, "me-id", *CONFIG)
    assert [pr["id"] for pr in buckets["to_review"]] == [1001]
    assert [pr["id"] for pr in buckets["my_changes_requested"]] == [1002]
    assert [pr["id"] for pr in buckets["my_approved_not_completed"]] == [1003]
    assert [pr["id"] for pr in buckets["my_waiting_on_others"]] == [1004]
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
    buckets = _client()._classify_prs(prs, "me-id", *CONFIG)
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
    client = _client(token)
    snapshot = client.snapshot()
    assert [(m, u) for m, u, _, _ in calls] == [
        ("GET", "https://dev.azure.com/example-org/_apis/connectionData?api-version=7.1-preview"),
        ("POST", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql?api-version=7.1"),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/workitems?ids=9&fields=System.Id%2CSystem.Title%2CSystem.State%2CSystem.WorkItemType%2CSystem.ChangedDate&api-version=7.1"),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/git/pullrequests?searchCriteria.status=active&%24top=500&api-version=7.1"),
    ]
    assert json.loads(calls[1][2]) == {"query": client._wiql(CONFIG[1])}
    assert all(header == basic for _, _, _, header in calls)
    assert snapshot["project_active_count"] == 4
    assert [p["id"] for p in snapshot["to_review"]] == [1001]
    assert snapshot["open_tasks_by_state"] == {"New": [snapshot["open_tasks"][0]]}


def test_ado_opener_rejects_redirects():
    assert ado._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test") is None
    assert not any(isinstance(handler, HTTPRedirectHandler) and not isinstance(handler, ado._NoRedirect)
                   for handler in ado._OPENER.handlers)


def test_token_is_absent_from_snapshot_api_html_and_safe_error(monkeypatch, tmp_path):
    from devmem.panel import app as panel

    token = "DISTINCTIVE-ADO-TOKEN-DO-NOT-LEAK"
    monkeypatch.setattr(ado, "_OPENER", _FailingOpener(token))
    cache = panel.AdoCache(client=_client(token))
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
    assert token not in repr(panel.AdoCache(client=_client(token)).refresh())


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
             "my_waiting_on_others": [{"id": 2, "title": "Wait", "pending_reviewers": ["Reviewer A"]}],
             "my_changes_requested": [{"id": 3, "title": "Gone"}]}
    second = {**empty_snapshot(True), "to_review": [{"id": 4, "title": "New review"}],
              "my_approved_not_completed": [{"id": 1, "title": "Review"}],
              "my_waiting_on_others": [{"id": 2, "title": "Wait", "pending_reviewers": ["Reviewer B"]}]}
    class Client:
        configured = lambda self: True
        values = [first, second]
        def snapshot(self): return self.values.pop(0)
    cache = AdoCache(Client())
    cache.refresh(); cache.refresh()
    assert [change["text"] for change in reversed(cache.get_changes()[1])] == [
        "PR !4 New review: now in to review", "PR !1 Review: now in approved, not completed", "PR !2 Wait: pending reviewers now Reviewer B",
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
    snapshot = _client(token=None).snapshot()
    assert snapshot == empty_snapshot(missing=["token"])
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
            return {**empty_snapshot(True), "to_review": [{"id": 7, "title": "Review me", "repo": "demo-repo",
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


@pytest.mark.parametrize(("content", "reason"), [
    ('{"organization":"file-org","project":"File Project"}', None),
    ('[]', "config file: not a JSON object"),
    ('{"organization":"file-org"}', "config file: missing key project"),
    ('{"project":"File"}', "config file: missing key organization"),
    ('{"organization":"file-org","project":"File","extra":1}', "config file: 1 unknown key(s) (allowed: organization, project)"),
    ('{"organization":1,"project":"File"}', "config file: organization must be a string"),
    ('{"organization":"bad/org","project":"File"}', "config file: organization is not a valid ADO organization name (letters, digits, hyphens; must start with a letter or digit; max 50)"),
    ("{\"organization\":\"file-org\",\"project\":\"bad'project\"}", "config file: project is not a valid ADO project name (letters, digits, spaces, . _ -; must start with a letter or digit; max 64)"),
])
def test_read_config_file_reasons_are_safe(tmp_path, monkeypatch, content, reason):
    path = tmp_path / "ado.json"; path.write_text(content)
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(path))
    config, actual = ado._read_config()
    assert (config is not None) == (reason is None)
    assert actual == reason
    if actual: assert "bad/org" not in actual and "bad'project" not in actual


def test_read_config_env_precedence_partial_empty_path_and_directory(tmp_path, monkeypatch):
    path = tmp_path / "ado.json"; path.write_text('{"organization":"file-org","project":"File Project"}')
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(path))
    monkeypatch.setenv("DEVMEM_ADO_ORG", "env-org")
    assert ado._read_config() == (("env-org", "File Project"), None)
    monkeypatch.delenv("DEVMEM_ADO_ORG"); monkeypatch.setenv("DEVMEM_ADO_PROJECT", "EnvProject")
    assert ado._read_config() == (("file-org", "EnvProject"), None)
    monkeypatch.setenv("DEVMEM_ADO_ORG", "")
    assert ado._read_config() == (("file-org", "EnvProject"), None)
    monkeypatch.setenv("DEVMEM_ADO_ORG", "bad/org")
    assert ado._read_config()[1].startswith("DEVMEM_ADO_ORG organization is not a valid")
    monkeypatch.setenv("DEVMEM_ADO_ORG", "env-org"); monkeypatch.setenv("DEVMEM_ADO_PROJECT", "EnvProject")
    path.write_text("{")
    assert ado._read_config() == (("env-org", "EnvProject"), None)
    monkeypatch.delenv("DEVMEM_ADO_ORG"); monkeypatch.delenv("DEVMEM_ADO_PROJECT")
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(tmp_path))
    assert ado._read_config()[1] == "config file: cannot be read (IsADirectoryError)"


def test_read_config_missing_and_tilde_override(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVMEM_ADO_CONFIG", raising=False)
    assert ado._read_config() == (None, None)
    home = tmp_path / "home"; home.mkdir(); path = home / "ado.json"
    path.write_text('{"organization":"example-org","project":"ExampleProject"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", "~/ado.json")
    assert ado._read_config() == (("example-org", "ExampleProject"), None)


def test_unconfigured_default_renders_not_configured_card(tmp_path, monkeypatch):
    from devmem.panel import app as panel
    monkeypatch.delenv("DEVMEM_ADO_CONFIG", raising=False)
    assert ado._read_config() == (None, None)
    cache = panel.AdoCache(AdoClient())
    cache.refresh()  # before the first refresh the page shows "Loading…"; unconfigured refreshes make no requests
    monkeypatch.setattr(panel, "_ado_cache", cache)
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "panel.db"))
    page = TestClient(panel.app, base_url="http://127.0.0.1").get("/ado").text
    assert "Azure DevOps is not configured" in page


def test_token_config_loader_and_request_safety(monkeypatch, caplog):
    token = "secret-token"; opener = _Opener(); monkeypatch.setattr(ado, "_OPENER", opener)
    for config in ((token, "ExampleProject"), ("example-org", token), ["example-org", "ExampleProject"], ("only",), ("example-org", 1)):
        client = _client(token, config)
        snapshot = client.snapshot()
        assert not snapshot["configured"] and token not in repr(snapshot)
    message = "do not log this message"  # kept off the raise line, which a traceback shows
    def broken(): raise RuntimeError(message)
    snapshot = AdoClient(lambda: token, broken).snapshot()
    assert snapshot["config_error"] == "config loader error (RuntimeError)"
    assert "RuntimeError" in caplog.text and "do not log this message" not in caplog.text
    for client in (_client(None), _client(token, None)):
        with pytest.raises(AdoError): client._request("GET", "https://example.test/nope")
    assert opener.calls == []


def test_config_is_snapshotted_once_links_and_allowlist_use_it(monkeypatch):
    configs = iter([CONFIG, ("other-org", "Other Project")])
    calls = []
    fixtures = json.loads((Path(__file__).parent / "fixtures" / "ado_prs.json").read_text())
    class Routes:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if "connectionData" in request.full_url: data = {"authenticatedUser": {"id": "me-id"}}
            elif "wiql" in request.full_url: data = {"workItems": [{"id": 9}]}
            elif "workitems" in request.full_url: data = {"value": [{"id": 9, "fields": {}}]}
            else: data = {"value": fixtures}
            return _Response(json.dumps(data).encode())
    monkeypatch.setattr(ado, "_OPENER", Routes())
    snapshot = AdoClient(lambda: "t", lambda: (next(configs), None)).snapshot()
    assert all("example-org" in url for url in calls)
    assert snapshot["open_tasks"][0]["url"] == "https://dev.azure.com/example-org/ExampleProject/_workitems/edit/9"
    assert snapshot["to_review"][0]["url"] == "https://dev.azure.com/example-org/ExampleProject/_git/demo-repo/pullrequest/1001"
    pr = _client()._pr({"pullRequestId": 1, "repository": {"name": "repo name"}}, "example-org", "Example Project")
    assert pr["url"] == "https://dev.azure.com/example-org/Example%20Project/_git/repo%20name/pullrequest/1"


def test_extra_allowlist_refusals_and_http_hint(monkeypatch):
    client = _client(); opener = _Opener(); monkeypatch.setattr(ado, "_OPENER", opener)
    urls = [
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql"),
        ("POST", "https://dev.azure.com/example-org/_apis/connectionData"),
        ("GET", "https://dev.azure.com/example-org/ExampleProject/_apis/git/pullrequests/"),
        ("GET", "https://dev.azure.com/EXAMPLE-ORG/ExampleProject/_apis/git/pullrequests"),
        ("GET", "https://dev.azure.com/example-org/Example+Project/_apis/git/pullrequests"),
        ("GET", "https://dev.azure.com/example-org/Example%2520Project/_apis/git/pullrequests"),
    ]
    for method, url in urls:
        with pytest.raises(AdoForbidden): client._request(method, url, b"{}" if method == "POST" else None)
    class NotFound:
        def open(self, request, timeout): raise HTTPError(request.full_url, 404, "", {}, BytesIO())
    monkeypatch.setattr(ado, "_OPENER", NotFound())
    with pytest.raises(AdoError, match="check organization/project in the ADO config") as exc:
        client._request("GET", "https://dev.azure.com/example-org/_apis/connectionData")
    assert "offline-test-token" not in str(exc.value)


def test_config_reason_is_rendered_and_logged_once(caplog, tmp_path, monkeypatch):
    from devmem.panel import app as panel
    class Client:
        configured = lambda self: False
        values = [empty_snapshot(config_error="broken config", missing=["config"]),
                  empty_snapshot(config_error="broken config", missing=["config"]),
                  empty_snapshot(config_error="other config", missing=["config"])]
        def snapshot(self): return self.values.pop(0)
    cache = panel.AdoCache(Client())
    cache.refresh(); cache.refresh(); cache.refresh()
    assert caplog.text.count("Azure DevOps config problem") == 2
    monkeypatch.setattr(panel, "_ado_cache", cache); monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "panel.db"))
    assert "other config" in TestClient(panel.app, base_url="http://127.0.0.1").get("/ado").text


def test_read_token_decode_error_and_ado_loop_survives(monkeypatch, tmp_path, caplog):
    path = tmp_path / "token"; path.write_bytes(b"\xff")
    monkeypatch.setattr(ado, "TOKEN_PATH", path)
    assert ado._read_token() is None
    from devmem.panel import app as panel
    class Cache:
        calls = 0
        def refresh(self):
            self.calls += 1
            raise RuntimeError("boom")
    class Stop:
        def wait(self, seconds): return True
    cache = Cache(); monkeypatch.setattr(panel, "_ado_cache", cache)
    panel._ado_loop(Stop())
    assert cache.calls == 1 and "Azure DevOps refresh loop error" in caplog.text


def test_real_loader_bad_file_reason_reaches_snapshot_and_page(tmp_path, monkeypatch):
    from devmem.panel import app as panel
    path = tmp_path / "ado.json"; path.write_text("{")
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(path))
    snapshot = AdoClient(lambda: "t").snapshot()
    assert snapshot["config_error"].startswith("config file: invalid JSON")
    assert "line 1" in snapshot["config_error"] and "column" in snapshot["config_error"]
    cache = panel.AdoCache(AdoClient(lambda: "t")); cache.refresh()
    monkeypatch.setattr(panel, "_ado_cache", cache)
    assert snapshot["config_error"] in TestClient(panel.app, base_url="http://127.0.0.1").get("/ado").text


def test_unconfigured_refresh_resets_baseline_without_journal_or_pulses():
    from devmem.panel.app import AdoCache
    good = {**empty_snapshot(True), "to_review": [{"id": 1}]}
    bad_config = empty_snapshot(config_error="config file: invalid JSON", missing=["config"])
    no_token = empty_snapshot(missing=["token"])
    class Client:
        configured = lambda self: True
        def __init__(self, values): self.values = values
        def snapshot(self): return self.values.pop(0)
    for interruption in (bad_config, no_token):
        pulses = []; cache = AdoCache(Client([good, interruption, good]), pulses.append)
        cache.refresh(); cache.refresh(); cache.refresh()
        assert cache.get_changes() == (0, []) and pulses == []


def test_file_and_loader_errors_do_not_expose_inputs(tmp_path, monkeypatch, caplog):
    secret = "DISTINCTIVE-KEY-TOKEN"
    path = tmp_path / "ado.json"; path.write_text(json.dumps({"organization": "x", "project": "y", secret: 1}))
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(path))
    reason = ado._read_config()[1]
    assert reason == "config file: 1 unknown key(s) (allowed: organization, project)"
    assert secret not in reason
    missing = tmp_path / "missing.json"; monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(missing))
    assert ado._read_config()[1] == "config file not found (DEVMEM_ADO_CONFIG)"
    def non_string_reason(): return (None, 42)
    assert AdoClient(lambda: "t", non_string_reason).snapshot()["config_error"] == "config loader returned an invalid result"
    def broken(): raise RuntimeError(secret)
    client = AdoClient(lambda: "t", broken)
    client.snapshot(); client.snapshot()
    assert caplog.text.count("ADO config loader failed (RuntimeError)") == 1 and secret not in caplog.text


def test_default_token_file_errors_are_visible_once(tmp_path, monkeypatch, caplog):
    from devmem.panel.app import AdoCache
    directory = tmp_path / "ado_token"; directory.mkdir()
    monkeypatch.setattr(ado, "TOKEN_PATH", directory)
    cache = AdoCache(AdoClient())
    assert cache.refresh()["token_error"] == "ado_token: cannot be read (IsADirectoryError)"
    cache.refresh()
    assert caplog.text.count("Azure DevOps token problem") == 1
    file = tmp_path / "bad_token"; file.write_bytes(b"\xff")
    monkeypatch.setattr(ado, "TOKEN_PATH", file)
    assert AdoClient().snapshot()["token_error"] == "ado_token: not UTF-8 text"


def test_unconfigured_request_precedes_allowlist_and_valid_config_refuses_bad_url(monkeypatch):
    opener = _Opener(); monkeypatch.setattr(ado, "_OPENER", opener)
    with pytest.raises(AdoError) as exc:
        _client(token=None)._request("GET", "https://example.test/nope")
    assert type(exc.value) is AdoError
    with pytest.raises(AdoError) as exc:
        _client(config=None)._request("GET", "https://example.test/nope")
    assert type(exc.value) is AdoError
    with pytest.raises(AdoForbidden):
        _client()._request("GET", "https://example.test/nope")
    assert opener.calls == []


def test_config_file_non_utf8_partial_env_and_page_reports_all_missing(tmp_path, monkeypatch):
    from devmem.panel import app as panel
    path = tmp_path / "broken.json"; path.write_bytes(b"\xff")  # not the default config path
    monkeypatch.setenv("DEVMEM_ADO_CONFIG", str(path))
    assert ado._read_config()[1] == "config file: cannot be read (UnicodeDecodeError)"
    monkeypatch.delenv("DEVMEM_ADO_CONFIG")
    monkeypatch.setenv("DEVMEM_ADO_ORG", "example-org")
    assert ado._read_config()[1] == "DEVMEM_ADO_PROJECT is not set and no config file exists"
    monkeypatch.setenv("DEVMEM_ADO_ORG", "bad/org")
    reason = ado._read_config()[1]
    assert "bad/org" not in reason
    snapshot = empty_snapshot(config_error="config file: broken", missing=["token", "config"])
    cache = panel.AdoCache(); cache.value = snapshot
    monkeypatch.setattr(panel, "_ado_cache", cache)
    page = TestClient(panel.app, base_url="http://127.0.0.1").get("/ado").text
    assert "config file: broken" in page and "no token" in page and "no organization/project" in page


def test_configured_failure_and_loop_error_are_visible():
    from devmem.panel import app as panel
    class Broken:
        def configured(self): raise RuntimeError("private detail")
        def snapshot(self): raise RuntimeError("private detail")
    cache = panel.AdoCache(Broken())
    assert cache.get()["config_error"] == "configuration check failed (RuntimeError)"
    assert cache.refresh()["error"] == "unexpected error (RuntimeError)"
    class Stop:
        def wait(self, seconds): return True
    def raising_refresh(): raise RuntimeError("private detail")
    cache.refresh = raising_refresh  # refresh() itself never raises; force the loop's own guard
    old = panel._ado_cache
    try:
        panel._ado_cache = cache
        panel._ado_loop(Stop())
        assert cache.get()["error"] == "unexpected refresh loop error (RuntimeError)"
    finally:
        panel._ado_cache = old


def test_failure_after_unconfigured_shows_the_error_not_the_old_hint():
    from devmem.panel import app as panel
    class Client:
        def __init__(self): self.calls = 0
        def configured(self): return self.calls > 0
        def snapshot(self):
            self.calls += 1
            if self.calls == 1: return empty_snapshot(missing=["config"])
            raise AdoError("HTTP 404 — check organization/project in the ADO config")
    cache = panel.AdoCache(Client())
    assert cache.refresh()["missing"] == ["config"]
    failed = cache.refresh()
    assert failed["configured"] and failed["missing"] == [] and "HTTP 404" in failed["error"]


def test_switching_organization_or_project_starts_a_new_baseline():
    from devmem.panel import app as panel
    def snap(scope, task_id, pr_id):
        s = empty_snapshot(True); s["scope"] = scope
        s["open_tasks"] = [{"id": task_id, "title": "t", "state": "Active"}]; s["to_review"] = [{"id": pr_id, "title": "p"}]
        return s
    class Client:
        def __init__(self): self.snaps = iter([snap("a/A", 1, 7), snap("b/B", 2, 8), snap("b/B", 3, 8)])
        def configured(self): return True
        def snapshot(self): return next(self.snaps)
    pulses = []; cache = panel.AdoCache(Client(), on_waiting_added=pulses.append)
    cache.refresh(); cache.refresh()
    assert cache.get_changes()[1] == [] and pulses == []
    cache.refresh()  # same scope again: real changes are journalled
    assert sorted(c["text"] for c in cache.get_changes()[1]) == ["Task #2 t: no longer open", "Task #3 t: new, Active"]


def test_loader_exception_needing_arguments_is_logged_by_type_without_message(caplog):
    secret = "value-from-config"
    def loader(): raise UnicodeDecodeError("utf-8", secret.encode(), 0, 1, "bad")
    snapshot = AdoClient(lambda: "t", loader).snapshot()
    assert snapshot["config_error"] == "config loader error (UnicodeDecodeError)"
    assert "UnicodeDecodeError" in caplog.text and secret not in caplog.text


def test_specific_errors_are_not_also_reported_as_missing():
    snapshot = AdoClient(lambda: "t", lambda: (None, "config file: invalid JSON (x at line 1 column 1)")).snapshot()
    assert snapshot["missing"] == [] and snapshot["config_error"].startswith("config file:")
    assert AdoClient(lambda: None, lambda: (None, None)).snapshot()["missing"] == ["token", "config"]


def test_configured_true_with_token_and_config():
    assert _client().configured() is True


def test_snapshot_with_a_space_in_the_project_quotes_every_path_and_link(monkeypatch):
    calls = []
    fixtures = json.loads((Path(__file__).parent / "fixtures" / "ado_prs.json").read_text())
    class Routes:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if "connectionData" in request.full_url: data = {"authenticatedUser": {"id": "me-id"}}
            elif "wiql" in request.full_url: data = {"workItems": [{"id": 9}]}
            elif "workitems" in request.full_url: data = {"value": [{"id": 9, "fields": {}}]}
            else: data = {"value": fixtures}
            return _Response(json.dumps(data).encode())
    monkeypatch.setattr(ado, "_OPENER", Routes())
    snapshot = _client(config=("example-org", "Example Project")).snapshot()
    assert snapshot["configured"] and len(calls) == 4
    assert all("/Example%20Project/" in url for url in calls if "connectionData" not in url)
    assert "/Example%20Project/_workitems/edit/9" in snapshot["open_tasks"][0]["url"]
    assert "/Example%20Project/_git/demo-repo/pullrequest/1001" in snapshot["to_review"][0]["url"]
