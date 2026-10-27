"""Offline coverage for the ADO boundary: no test ever contacts Azure DevOps."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError
import pytest
from fastapi.testclient import TestClient

from devmem import ado
from devmem.ado import AdoClient, AdoForbidden, empty_snapshot


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
    def open(self, request, timeout):
        raise URLError("offline test")


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


def test_token_is_absent_from_snapshot_api_html_and_safe_error(monkeypatch, tmp_path):
    from devmem.panel import app as panel

    token = "DISTINCTIVE-ADO-TOKEN-DO-NOT-LEAK"
    monkeypatch.setattr(ado, "_OPENER", _FailingOpener())
    cache = panel.AdoCache(client=AdoClient(lambda: token))
    snapshot = cache.refresh()
    assert token not in repr(snapshot)
    assert token not in (snapshot["error"] or "")
    monkeypatch.setattr(panel, "_ado_cache", cache)
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "panel.db"))
    with TestClient(panel.app) as test_client:
        assert token not in test_client.get("/api/ado").text
        assert token not in test_client.get("/ado").text


def test_not_configured_makes_zero_requests(monkeypatch):
    opener = _Opener()
    monkeypatch.setattr(ado, "_OPENER", opener)
    snapshot = AdoClient(lambda: None).snapshot()
    assert snapshot == empty_snapshot()
    assert opener.calls == []
