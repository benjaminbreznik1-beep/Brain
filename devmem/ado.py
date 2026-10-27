"""Strictly read-only Azure DevOps data for the local panel.

This module deliberately has a very small network surface.  Keep every ADO
request in :meth:`AdoClient._request`; that makes the allowlist auditable and
prevents a future caller from accidentally gaining write access.
The allowlist is rebuilt for every request from the organization/project that
the refresh resolved once; the host, methods, and endpoint shapes remain fixed.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import socket
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


TOKEN_PATH = Path.home() / ".devmem" / "ado_token"
CONFIG_PATH = Path.home() / ".devmem" / "ado.json"
TIMEOUT_S = 15
API_VERSION = "7.1"
_ORGANIZATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,49}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
log = logging.getLogger(__name__)


class AdoError(RuntimeError):
    """A safe-to-display ADO failure (its message never includes credentials)."""


class AdoForbidden(AdoError):
    """Raised without network I/O for a method, origin, path, or body outside the allowlist."""


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


def _read_token_status() -> tuple[str | None, str | None]:
    """Load the local token at runtime without exposing its contents."""
    try:
        value = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, None
    except UnicodeDecodeError:
        return None, "ado_token: not UTF-8 text"
    except OSError as exc:
        return None, f"ado_token: cannot be read ({type(exc).__name__})"
    return value or None, None


def _read_token() -> str | None:
    """Default token loader: the token only, without the read-error reason."""
    return _read_token_status()[0]


def _validate_config(value: object, token: str | None = None, sources: dict[str, str] | None = None) -> tuple[tuple[str, str] | None, str | None]:
    """Validate a pair without ever including its values in an error."""
    if not isinstance(value, tuple) or len(value) != 2:
        return None, "config must be an organization/project tuple"
    organization, project = value
    for name, item, pattern, rule in (
        ("organization", organization, _ORGANIZATION_RE, "letters, digits, hyphens; must start with a letter or digit; max 50"),
        ("project", project, _PROJECT_RE, "letters, digits, spaces, . _ -; must start with a letter or digit; max 64"),
    ):
        prefix = f"{(sources or {}).get(name, '')} " if (sources or {}).get(name) else ""
        if not isinstance(item, str):
            return None, f"{prefix}{name} must be a string"
        if token is not None and item == token:
            return None, f"{prefix}{name} must not be the token"
        if not pattern.fullmatch(item):
            return None, f"{prefix}{name} is not a valid ADO {name} name ({rule})"
    return (organization, project), None


def _read_config() -> tuple[tuple[str, str] | None, str | None]:
    """Read env fields with file fallback, including an expandable path override and safe reasons.

    Each non-empty env field overrides only that field.  Missing fields fall back
    to ``DEVMEM_ADO_CONFIG`` (or the default file); all malformed input is
    reported as a reason without exposing configured values.
    """
    organization = os.environ.get("DEVMEM_ADO_ORG") or None
    project = os.environ.get("DEVMEM_ADO_PROJECT") or None
    override = os.environ.get("DEVMEM_ADO_CONFIG") or None
    path = Path(override).expanduser() if override else CONFIG_PATH
    if override and not path.exists():
        return None, "config file not found (DEVMEM_ADO_CONFIG)"
    value: object = {}
    if organization is None or project is None:
        try:
            text = path.read_text(encoding="utf-8")
            value = json.loads(text)
        except FileNotFoundError:
            if override:
                return None, "config file not found (DEVMEM_ADO_CONFIG)"
            if organization is None and project is None:
                return None, None
            missing = "DEVMEM_ADO_ORG" if organization is None else "DEVMEM_ADO_PROJECT"
            return None, f"{missing} is not set and no config file exists"
        except json.JSONDecodeError as exc:
            return None, f"config file: invalid JSON ({exc.msg} at line {exc.lineno} column {exc.colno})"
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            return None, f"config file: cannot be read ({type(exc).__name__})"
        if not isinstance(value, dict):
            return None, "config file: not a JSON object"
        unknown = set(value) - {"organization", "project"}
        if unknown:
            return None, f"config file: {len(unknown)} unknown key(s) (allowed: organization, project)"
        if organization is None:
            if "organization" not in value:
                return None, "config file: missing key organization"
            organization = value["organization"]
        if project is None:
            if "project" not in value:
                return None, "config file: missing key project"
            project = value["project"]
    sources = {
        "organization": "DEVMEM_ADO_ORG" if os.environ.get("DEVMEM_ADO_ORG") else "config file:",
        "project": "DEVMEM_ADO_PROJECT" if os.environ.get("DEVMEM_ADO_PROJECT") else "config file:",
    }
    return _validate_config((organization, project), sources=sources)


def empty_snapshot(configured: bool = False, config_error: str | None = None, missing: list[str] | None = None,
                   token_error: str | None = None) -> dict[str, Any]:
    """The stable public shape used before the first successful refresh too."""
    return {
        "configured": configured,
        "config_error": config_error,
        "token_error": token_error,
        "missing": missing or [],
        "scope": None,
        "pending": False,
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

    def __init__(self, token_loader: Callable[[], str | None] = _read_token,
                 config_loader: Callable[[], object] = _read_config):
        self._token_loader = token_loader
        self._config_loader = config_loader
        self._config_loader_failures: set[type[BaseException]] = set()

    def _token(self) -> tuple[str | None, str | None]:
        if self._token_loader is _read_token:
            return _read_token_status()
        return self._token_loader(), None

    def _config(self, token: str | None = None) -> tuple[tuple[str, str] | None, str | None]:
        """Re-validate loader output; loader errors are logged by type and disable ADO.

        Loaders return only the normal ``(config, reason)`` result.
        """
        try:
            value = self._config_loader()
        except Exception as exc:
            if type(exc) not in self._config_loader_failures:
                # Frames only: the exception message could contain a config value.
                log.error("ADO config loader failed (%s)\n%s", type(exc).__name__,
                          "".join(traceback.format_tb(exc.__traceback__)).rstrip())
                self._config_loader_failures.add(type(exc))
            return None, f"config loader error ({type(exc).__name__})"
        if (isinstance(value, tuple) and len(value) == 2 and
                (value[0] is None or isinstance(value[0], tuple))):
            config, reason = value
            if config is None:
                # (None, None) is the normal "nothing configured" result, not an error.
                return None, reason if reason is None or isinstance(reason, str) else "config loader returned an invalid result"
            validated, validation_reason = _validate_config(config, token)
            if reason is not None and not isinstance(reason, str):
                return None, "config loader returned an invalid result"
            return validated, validation_reason or reason
        return None, "config loader returned an invalid result"

    @staticmethod
    def _wiql(project: str) -> str:
        # Defence in depth: validation already rejects apostrophes.
        project = project.replace("'", "''")
        return ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = '" + project + "' "
                "AND [System.AssignedTo] = @Me AND [System.State] NOT IN "
                "('Closed','Done','Removed') ORDER BY [System.ChangedDate] DESC")

    @staticmethod
    def _paths(config: tuple[str, str]) -> tuple[frozenset[str], frozenset[str]]:
        organization, project = config
        prefix = f"/{organization}/{quote(project, safe='')}"
        return (frozenset({f"/{organization}/_apis/connectionData", f"{prefix}/_apis/wit/workitems",
                           f"{prefix}/_apis/git/pullrequests"}),
                frozenset({f"{prefix}/_apis/wit/wiql"}))

    def configured(self) -> bool:
        """Whether both a token and valid organization/project are present, without exposing them."""
        token, _ = self._token()
        config, _ = self._config(token)
        return bool(token and config)

    def _request(self, method: str, url: str, body: bytes | None = None, *, config: tuple[str, str] | None = None) -> dict[str, Any]:
        """Validate first, then make one authenticated request with no redirects."""
        token, _ = self._token()
        if config is None:
            config, reason = self._config(token)
        else:
            reason = None
        if not token or config is None:
            raise AdoError(reason or "ADO not configured (missing token or organization/project)")
        method = method.upper()
        parts = urlsplit(url)
        # netloc rather than hostname rejects userinfo and alternate ports as well.
        valid_origin = parts.scheme == "https" and parts.netloc == "dev.azure.com"
        get_paths, post_paths = self._paths(config)
        valid_path = ((method == "GET" and parts.path in get_paths)
                      or (method == "POST" and parts.path in post_paths))
        if not valid_origin or not valid_path:
            raise AdoForbidden("ADO request is outside the read-only allowlist")
        if method == "GET" and body is not None:
            raise AdoForbidden("ADO GET requests cannot have a body")
        if method == "POST" and (parts.path not in post_paths or body is None):
            raise AdoForbidden("ADO POST is limited to the WIQL query")
        if method == "POST":
            try:
                is_wiql = json.loads(body.decode("utf-8")) == {"query": self._wiql(config[1])}
            except (UnicodeDecodeError, json.JSONDecodeError):
                is_wiql = False
            if not is_wiql:
                raise AdoForbidden("ADO POST is limited to the fixed WIQL query")

        authorization = base64.b64encode((":" + token).encode("utf-8")).decode("ascii")
        headers = {"Authorization": "Basic " + authorization, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with _OPENER.open(request, timeout=TIMEOUT_S) as response:
                status = getattr(response, "status", None)
                getcode = getattr(response, "getcode", None)
                if status is None and getcode:
                    status = getcode()
                content_type = getattr(response, "headers", {}).get("Content-Type", "")
                if status == 203 and "html" in content_type.lower():
                    raise AdoError("HTTP 203 — token expired or missing scope?")
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            message = f"HTTP {exc.code}"
            if exc.code == 401:
                message += " — token expired, missing scope, or wrong organization/project?"
            elif exc.code in (203, 403):
                message += " — token expired or missing scope?"
            if exc.code in (302, 404):
                message += " — check organization/project in the ADO config"
            raise AdoError(message) from None
        except (TimeoutError, socket.timeout):
            raise AdoError("timeout") from None
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise AdoError("timeout") from None
            raise AdoError(f"network error ({type(exc).__name__})") from None
        except OSError as exc:
            raise AdoError(f"network error ({type(exc).__name__})") from None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise AdoError("invalid JSON") from None

    @staticmethod
    def _url(path: str, **query: Any) -> str:
        # Paths already start with /<organization>, as the allowlist expects.
        query.setdefault("api-version", API_VERSION)
        return "https://dev.azure.com" + path + "?" + urlencode(query, doseq=True)

    def _get(self, path: str, *, config: tuple[str, str], **query: Any) -> dict[str, Any]:
        return self._request("GET", self._url(path, **query), config=config)

    def _post_wiql(self, config: tuple[str, str]) -> dict[str, Any]:
        organization, project = config
        body = json.dumps({"query": self._wiql(project)}, separators=(",", ":")).encode("utf-8")
        path = f"/{organization}/{quote(project, safe='')}/_apis/wit/wiql"
        return self._request("POST", self._url(path), body, config=config)

    def snapshot(self) -> dict[str, Any]:
        """Fetch and classify ADO data, or report disabled without network I/O."""
        token, token_error = self._token()
        config, reason = self._config(token)
        # A specific token/config error already says what to fix; "missing" is only for absent ones.
        missing = ([] if token or token_error else ["token"]) + ([] if config or reason else ["config"])
        if not token or config is None:
            return empty_snapshot(config_error=reason, token_error=token_error, missing=missing)

        organization, project = config
        project_path = quote(project, safe="")

        connection = self._get(f"/{organization}/_apis/connectionData", config=config, **{"api-version": API_VERSION + "-preview"})  # preview-only API
        person = connection.get("authenticatedUser") or {}
        my_id = person.get("id")
        if not my_id:
            raise AdoError("Azure DevOps identity was unavailable")
        me = {"id": my_id, "name": person.get("providerDisplayName") or connection.get("providerDisplayName")
              or person.get("displayName") or "Me"}

        wiql = self._post_wiql(config)
        ids = [str(row["id"]) for row in wiql.get("workItems", []) if row.get("id") is not None]
        work_items: list[dict[str, Any]] = []
        fields = "System.Id,System.Title,System.State,System.WorkItemType,System.ChangedDate"
        for start in range(0, len(ids), 200):
            data = self._get(f"/{organization}/{project_path}/_apis/wit/workitems", config=config, ids=",".join(ids[start:start + 200]), fields=fields)
            work_items.extend(self._work_item(item, organization, project) for item in data.get("value", []))

        prs = self._get(f"/{organization}/{project_path}/_apis/git/pullrequests", config=config, **{"searchCriteria.status": "active", "$top": 500})
        classified = self._classify_prs(prs.get("value", []), my_id, organization, project)
        tasks_by_state: dict[str, list[dict[str, Any]]] = {}
        for item in work_items:
            tasks_by_state.setdefault(item["state"] or "Unknown", []).append(item)
        return {
            "configured": True,
            "config_error": None,
            "token_error": None,
            "missing": [],
            "scope": f"{organization}/{project}",
            "refreshed_at": _iso_now(),
            "stale": False,
            "error": None,
            "me": me,
            "project_active_count": len(prs.get("value", [])),
            **classified,
            "open_tasks": work_items,
            "open_tasks_by_state": tasks_by_state,
        }

    def _work_item(self, item: dict[str, Any], organization: str, project: str) -> dict[str, Any]:
        fields = item.get("fields") or {}
        work_id = item.get("id") or fields.get("System.Id")
        return {
            "id": work_id,
            "title": fields.get("System.Title") or "Untitled work item",
            "type": fields.get("System.WorkItemType") or "",
            "state": fields.get("System.State") or "",
            "changed_date": fields.get("System.ChangedDate"),
            "url": f"https://dev.azure.com/{organization}/{quote(project, safe='')}/_workitems/edit/{work_id}",
        }

    def _pr(self, raw: dict[str, Any], organization: str, project: str) -> dict[str, Any]:
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
            "url": f"https://dev.azure.com/{organization}/{quote(project, safe='')}/_git/{quote(repo, safe='')}/pullrequest/{pr_id}",
            "created_date": created,
            "age_seconds": age_seconds,
            "age_label": _age_label(age_seconds),
            "reviewers": reviewers,
        }

    def _classify_prs(self, values: list[dict[str, Any]], my_id: str, organization: str, project: str) -> dict[str, list[dict[str, Any]]]:
        buckets = {
            "to_review": [], "my_changes_requested": [], "my_approved_not_completed": [], "my_waiting_on_others": [],
        }
        for raw in values:
            author_id = (raw.get("createdBy") or {}).get("id")
            reviewers = raw.get("reviewers") or []
            mine = author_id == my_id
            my_review = next((reviewer for reviewer in reviewers if reviewer.get("id") == my_id), None)
            pr = self._pr(raw, organization, project)
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
