"""Strictly read-only Azure DevOps data for the local panel.

This module deliberately has a very small network surface.  Keep every ADO
request in :meth:`AdoClient._request`; that makes the allowlist auditable and
prevents a future caller from accidentally gaining write access.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


BASE_URL = "https://dev.azure.com/example-org"
TOKEN_PATH = Path.home() / ".devmem" / "ado_token"
TIMEOUT_S = 15
API_VERSION = "7.1"
WIQL = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = 'AI' "
        "AND [System.AssignedTo] = @Me AND [System.State] NOT IN "
        "('Closed','Done','Removed') ORDER BY [System.ChangedDate] DESC")

_GET_PATHS = frozenset({
    "/example-org/_apis/connectionData",
    "/example-org/ExampleProject/_apis/wit/workitems",
    "/example-org/ExampleProject/_apis/git/pullrequests",
})
_POST_PATHS = frozenset({"/example-org/ExampleProject/_apis/wit/wiql"})


class AdoError(RuntimeError):
    """A safe-to-display ADO failure (its message never includes credentials)."""


class AdoForbidden(AdoError):
    """Raised before network access for a method or URL outside the allowlist."""


class _NoRedirect(HTTPRedirectHandler):
    """Redirects could escape an otherwise safe URL; reject every one of them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = build_opener(_NoRedirect())


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _age_label(seconds: int | None) -> str:
    if seconds is None:
        return "age unknown"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m old"
    if seconds < 86400:
        return f"{seconds // 3600}h old"
    return f"{seconds // 86400}d old"


def _read_token() -> str | None:
    """Load the local token only at runtime; missing and empty mean disabled."""
    try:
        value = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def empty_snapshot(configured: bool = False) -> dict[str, Any]:
    """The stable public shape used before the first successful refresh too."""
    return {
        "configured": configured,
        "refreshed_at": None,
        "stale": False,
        "error": None,
        "me": None,
        "project_active_count": 0,
        "to_review": [],
        "my_changes_requested": [],
        "my_approved_not_completed": [],
        "my_waiting_on_others": [],
        "open_tasks": [],
        "open_tasks_by_state": {},
    }


class AdoClient:
    """Read-only client for exactly the panel's allowed ADO endpoints."""

    def __init__(self, token_loader: Callable[[], str | None] = _read_token):
        self._token_loader = token_loader

    def _request(self, method: str, url: str, body: bytes | None = None) -> dict[str, Any]:
        """Validate first, then make one authenticated request with no redirects."""
        method = method.upper()
        parts = urlsplit(url)
        # netloc rather than hostname rejects userinfo and alternate ports as well.
        valid_origin = parts.scheme == "https" and parts.netloc == "dev.azure.com"
        valid_path = ((method == "GET" and parts.path in _GET_PATHS)
                      or (method == "POST" and parts.path in _POST_PATHS))
        if not valid_origin or not valid_path:
            raise AdoForbidden("ADO request is outside the read-only allowlist")
        if method == "GET" and body is not None:
            raise AdoForbidden("ADO GET requests cannot have a body")
        if method == "POST" and (parts.path not in _POST_PATHS or body is None):
            raise AdoForbidden("ADO POST is limited to the WIQL query")
        if method == "POST":
            try:
                is_wiql = json.loads(body.decode("utf-8")) == {"query": WIQL}
            except (UnicodeDecodeError, json.JSONDecodeError):
                is_wiql = False
            if not is_wiql:
                raise AdoForbidden("ADO POST is limited to the fixed WIQL query")

        token = self._token_loader()
        if not token:
            raise AdoError("Azure DevOps is not configured")
        authorization = base64.b64encode((":" + token).encode("utf-8")).decode("ascii")
        headers = {"Authorization": "Basic " + authorization, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with _OPENER.open(request, timeout=TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
            # Do not use str(exc): an upstream error can contain echoed request data.
            raise AdoError("Azure DevOps request failed") from None

    @staticmethod
    def _url(path: str, **query: Any) -> str:
        query.setdefault("api-version", API_VERSION)
        return BASE_URL + path + "?" + urlencode(query, doseq=True)

    def _get(self, path: str, **query: Any) -> dict[str, Any]:
        return self._request("GET", self._url(path, **query))

    def _post_wiql(self) -> dict[str, Any]:
        body = json.dumps({"query": WIQL}, separators=(",", ":")).encode("utf-8")
        return self._request("POST", self._url("/AI/_apis/wit/wiql"), body)

    def snapshot(self) -> dict[str, Any]:
        """Fetch and classify the ADO data, or report locally-disabled without I/O."""
        if not self._token_loader():
            return empty_snapshot()

        connection = self._get("/_apis/connectionData", **{"api-version": API_VERSION + "-preview"})  # preview-only API
        person = connection.get("authenticatedUser") or {}
        my_id = person.get("id")
        if not my_id:
            raise AdoError("Azure DevOps identity was unavailable")
        me = {"id": my_id, "name": person.get("providerDisplayName") or connection.get("providerDisplayName")
              or person.get("displayName") or "Me"}

        wiql = self._post_wiql()
        ids = [str(row["id"]) for row in wiql.get("workItems", []) if row.get("id") is not None]
        work_items: list[dict[str, Any]] = []
        fields = "System.Id,System.Title,System.State,System.WorkItemType,System.ChangedDate"
        for start in range(0, len(ids), 200):
            data = self._get("/AI/_apis/wit/workitems", ids=",".join(ids[start:start + 200]), fields=fields)
            work_items.extend(self._work_item(item) for item in data.get("value", []))

        prs = self._get("/AI/_apis/git/pullrequests", **{"searchCriteria.status": "active", "$top": 500})
        classified = self._classify_prs(prs.get("value", []), my_id)
        tasks_by_state: dict[str, list[dict[str, Any]]] = {}
        for item in work_items:
            tasks_by_state.setdefault(item["state"] or "Unknown", []).append(item)
        return {
            "configured": True,
            "refreshed_at": _iso_now(),
            "stale": False,
            "error": None,
            "me": me,
            "project_active_count": len(prs.get("value", [])),
            **classified,
            "open_tasks": work_items,
            "open_tasks_by_state": tasks_by_state,
        }

    @staticmethod
    def _work_item(item: dict[str, Any]) -> dict[str, Any]:
        fields = item.get("fields") or {}
        work_id = item.get("id") or fields.get("System.Id")
        return {
            "id": work_id,
            "title": fields.get("System.Title") or "Untitled work item",
            "type": fields.get("System.WorkItemType") or "",
            "state": fields.get("System.State") or "",
            "changed_date": fields.get("System.ChangedDate"),
            "url": f"{BASE_URL}/AI/_workitems/edit/{work_id}",
        }

    @staticmethod
    def _pr(raw: dict[str, Any]) -> dict[str, Any]:
        pr_id = raw.get("pullRequestId")
        repo = (raw.get("repository") or {}).get("name") or ""
        reviewers = [{
            "id": reviewer.get("id"),
            "name": reviewer.get("displayName") or reviewer.get("uniqueName") or "Unknown reviewer",
            "vote": int(reviewer.get("vote") or 0),
        } for reviewer in raw.get("reviewers", [])]
        created = raw.get("creationDate")
        age_seconds = None
        if isinstance(created, str):
            try:
                parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_seconds = max(0, int((_utcnow() - parsed).total_seconds()))
            except ValueError:
                pass
        return {
            "id": pr_id,
            "title": raw.get("title") or "Untitled pull request",
            "repo": repo,
            "url": f"{BASE_URL}/AI/_git/{repo}/pullrequest/{pr_id}",
            "created_date": created,
            "age_seconds": age_seconds,
            "age_label": _age_label(age_seconds),
            "reviewers": reviewers,
        }

    @classmethod
    def _classify_prs(cls, values: list[dict[str, Any]], my_id: str) -> dict[str, list[dict[str, Any]]]:
        buckets = {
            "to_review": [], "my_changes_requested": [], "my_approved_not_completed": [], "my_waiting_on_others": [],
        }
        for raw in values:
            author_id = (raw.get("createdBy") or {}).get("id")
            reviewers = raw.get("reviewers") or []
            mine = author_id == my_id
            my_review = next((reviewer for reviewer in reviewers if reviewer.get("id") == my_id), None)
            pr = cls._pr(raw)
            if not mine and my_review is not None and int(my_review.get("vote") or 0) == 0:
                buckets["to_review"].append(pr)
            if not mine:
                continue
            votes = [int(reviewer.get("vote") or 0) for reviewer in reviewers]
            if any(vote < 0 for vote in votes):
                buckets["my_changes_requested"].append(pr)
            elif any(vote >= 5 for vote in votes):
                buckets["my_approved_not_completed"].append(pr)
            else:
                pr["pending_reviewers"] = [reviewer["name"] for reviewer in pr["reviewers"] if reviewer["vote"] == 0]
                buckets["my_waiting_on_others"].append(pr)
        return buckets
