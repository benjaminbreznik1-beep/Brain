"""Remote access: the mcp-http server (MCP over streamable HTTP + the SDK's OAuth server) for claude.ai custom connectors.

Codes and tokens are stored as SHA-256 hashes only; the passphrase as scrypt in remote.json. None of them are ever logged."""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import inspect
import json
import logging
import os
import secrets
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from markupsafe import Markup
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken, RegistrationError, TokenError,
                                      construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .db import connection, now

log = logging.getLogger("devmem.remote")

DEFAULT_REDIRECTS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
SCOPE = "devmem"
DEFAULT_PORT = 8766
CODE_TTL_S, ACCESS_TTL_S, REFRESH_TTL_S, PENDING_TTL_S = 300, 3600, 30 * 86400, 600
MAX_PENDING = 100
REMOTE_TOOLS = ("memory_search", "memory_get", "memory_list", "memory_history", "project_context", "maintenance_status", "ado_status",
                "memory_save", "memory_link", "message_post", "message_inbox", "message_thread", "message_wait", "message_close")
REMOTE_NOTE = "Remote connector: update, delete and restore are only available locally."
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1


# ---------------------------------------------------------------- config (~/.devmem/remote.json, never .env)

def config_path() -> Path:
    return Path(os.environ.get("DEVMEM_REMOTE", "~/.devmem/remote.json")).expanduser()


def load_config() -> dict[str, Any]:
    """The remote config, or {} when there is none. A malformed file raises ValueError."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{config_path()} is unreadable ({type(exc).__name__})") from exc
    if not isinstance(data, dict): raise ValueError(f"{config_path()} must hold a JSON object")
    return data


def save_config(cfg: dict[str, Any]) -> None:
    """Write atomically with mode 0600: the file holds the passphrase hash."""
    path = config_path(); path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2); f.write("\n")
        os.chmod(partial, 0o600); os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True); raise


def redirect_allowlist(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = load_config() if cfg is None else cfg
    allowed = cfg.get("redirect_allowlist")
    return [u for u in allowed if isinstance(u, str)] if isinstance(allowed, list) else list(DEFAULT_REDIRECTS)


def setup_problem(cfg: dict[str, Any]) -> str | None:
    """Why mcp-http must not start with this config, or None."""
    if not cfg.get("enabled"): return "remote access is not enabled"
    if not isinstance(cfg.get("public_url"), str) or not cfg["public_url"].startswith("https://"): return "remote.json has no https public_url"
    if not cfg.get("passphrase_scrypt"): return "no passphrase is set"
    return None


def normalise_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname: raise ValueError("the public URL must be https://<host>[:port]")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"): raise ValueError("the public URL must not have a path, query or fragment")
    return f"https://{parsed.netloc}"


_hosts_cache: tuple[Any, list[str]] = (None, [])


def panel_hosts() -> list[str]:
    """remote.json panel_hosts (Tailscale Serve names), re-read only when the file changes."""
    global _hosts_cache
    path = config_path()
    try: key = (str(path), path.stat().st_mtime_ns)
    except OSError: return []
    if _hosts_cache[0] != key:
        try: hosts = load_config().get("panel_hosts")
        except ValueError: hosts = None
        _hosts_cache = (key, [h for h in hosts if isinstance(h, str) and h and h != "*"] if isinstance(hosts, list) else [])
    return list(_hosts_cache[1])


# ---------------------------------------------------------------- passphrase

def hash_passphrase(passphrase: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(passphrase.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=2 ** 26, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_passphrase(passphrase: str, stored: str | None) -> bool:
    try:
        name, n, r, p, salt, expected = (stored or "").split("$")
        if name != "scrypt": return False
        expected_bytes = base64.b64decode(expected)
        digest = hashlib.scrypt(passphrase.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=2 ** 26, dklen=len(expected_bytes))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected_bytes)


class AttemptLimiter:
    """Wrong passphrases: 5 per 15 min per source IP or 20 per hour overall, then every attempt is refused for 15 min."""
    PER_IP, PER_IP_WINDOW_S, GLOBAL, GLOBAL_WINDOW_S, LOCKOUT_S = 5, 900, 20, 3600, 900

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.failures: dict[str, deque[float]] = {}
        self.all_failures: deque[float] = deque()
        self.blocked_until: dict[str, float] = {}
        self.all_blocked_until = 0.0

    def _prune(self, t: float) -> None:
        while self.all_failures and self.all_failures[0] <= t - self.GLOBAL_WINDOW_S: self.all_failures.popleft()
        for ip in list(self.failures):
            times = self.failures[ip]
            while times and times[0] <= t - self.PER_IP_WINDOW_S: times.popleft()
            if not times: del self.failures[ip]
        for ip in [ip for ip, until in self.blocked_until.items() if until <= t]: del self.blocked_until[ip]

    def blocked(self, ip: str) -> bool:
        with self.lock:
            t = self.clock(); self._prune(t)
            return self.all_blocked_until > t or self.blocked_until.get(ip, 0.0) > t

    def failed(self, ip: str) -> None:
        with self.lock:
            t = self.clock(); self._prune(t)
            times = self.failures.setdefault(ip, deque()); times.append(t); self.all_failures.append(t)
            if len(times) >= self.PER_IP: self.blocked_until[ip] = t + self.LOCKOUT_S
            if len(self.all_failures) >= self.GLOBAL: self.all_blocked_until = t + self.LOCKOUT_S


def source_ip(request) -> str:
    """The peer address; X-Forwarded-For is trusted only from 127.0.0.1 (the Funnel proxy), and then only its last hop."""
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for")
    if peer == "127.0.0.1" and forwarded:
        return forwarded.split(",")[-1].strip() or peer
    return peer


# ---------------------------------------------------------------- storage

def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _issue_tokens(conn, client_id: str, scopes: list[str], resource: str | None, parent: str | None) -> OAuthToken:
    """A refresh token (child of `parent` when rotated) and an access token paired with it via parent_hash."""
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    t, refresh_hash, scope = int(time.time()), _hash(refresh), " ".join(scopes)
    conn.execute("INSERT INTO oauth_tokens(token_hash,kind,client_id,scopes,resource,expires_at,created_at,parent_hash) VALUES(?,?,?,?,?,?,?,?)",
                 (refresh_hash, "refresh", client_id, scope, resource, t + REFRESH_TTL_S, now(), parent))
    conn.execute("INSERT INTO oauth_tokens(token_hash,kind,client_id,scopes,resource,expires_at,created_at,parent_hash) VALUES(?,?,?,?,?,?,?,?)",
                 (_hash(access), "access", client_id, scope, resource, t + ACCESS_TTL_S, now(), refresh_hash))
    return OAuthToken(access_token=access, expires_in=ACCESS_TTL_S, scope=scope, refresh_token=refresh)


def _revoke_chain(conn, token_hash: str) -> None:
    """Revoke every token descended from the root of `token_hash`'s rotation chain (refresh tokens and their access tokens)."""
    root, seen = token_hash, {token_hash}
    while True:
        row = conn.execute("SELECT parent_hash FROM oauth_tokens WHERE token_hash=?", (root,)).fetchone()
        if not row or not row[0] or row[0] in seen: break
        root = row[0]; seen.add(root)
    conn.execute("""WITH RECURSIVE chain(h) AS (SELECT ? UNION SELECT t.token_hash FROM oauth_tokens t JOIN chain c ON t.parent_hash=c.h)
        UPDATE oauth_tokens SET revoked_at=? WHERE revoked_at IS NULL AND token_hash IN (SELECT h FROM chain)""", (root, now()))


def revoke_client(client_id: str) -> bool:
    """Revoke one client and all of its tokens; False when no active client has that id."""
    with connection() as conn:
        cur = conn.execute("UPDATE oauth_clients SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL", (now(), client_id))
        conn.execute("UPDATE oauth_tokens SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL", (now(), client_id))
        conn.execute("UPDATE agent_clients SET ended_at=? WHERE remote_client_id=? AND ended_at IS NULL", (now(), client_id))
        return bool(cur.rowcount)


def revoke_all_clients() -> int:
    with connection() as conn:
        ids = [row[0] for row in conn.execute("SELECT client_id FROM oauth_clients WHERE revoked_at IS NULL")]
    return sum(revoke_client(client_id) for client_id in ids)


def revoke_all_tokens() -> int:
    with connection() as conn:
        return conn.execute("UPDATE oauth_tokens SET revoked_at=? WHERE revoked_at IS NULL", (now(),)).rowcount


def clients() -> list[dict[str, Any]]:
    """Registered clients with their active (unrevoked, unexpired) token counts; never token values."""
    with connection() as conn:
        return [dict(row) for row in conn.execute("""SELECT c.client_id,c.client_name,c.created_at,c.last_used_at,c.revoked_at,
            SUM(t.kind='access' AND t.revoked_at IS NULL AND t.expires_at>?) access_tokens,
            SUM(t.kind='refresh' AND t.revoked_at IS NULL AND t.expires_at>?) refresh_tokens
            FROM oauth_clients c LEFT JOIN oauth_tokens t ON t.client_id=c.client_id
            GROUP BY c.client_id ORDER BY c.revoked_at IS NOT NULL, COALESCE(c.last_used_at,c.created_at) DESC""", (int(time.time()),) * 2)]


def remote_calls(limit: int = 50) -> list[dict[str, Any]]:
    with connection() as conn:
        return [dict(row) for row in conn.execute("""SELECT t.*,a.client_name FROM tool_calls t JOIN agent_clients a ON a.id=t.client_id
            WHERE a.remote_client_id IS NOT NULL ORDER BY t.id DESC LIMIT ?""", (limit,))]


def mcp_http_running(port: int | None = None) -> bool:
    port = port or int(os.environ.get("DEVMEM_MCP_HTTP_PORT", DEFAULT_PORT))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3): return True
    except OSError:
        return False


def agent_client(client_id: str) -> tuple[int, str]:
    """The agent_clients row standing for one OAuth client (pid, cwd and project NULL), created on its first tool call."""
    with connection() as conn:
        row = conn.execute("SELECT id,client_name FROM agent_clients WHERE remote_client_id=? ORDER BY id DESC LIMIT 1", (client_id,)).fetchone()
        if row: return row[0], row[1]
        named = conn.execute("SELECT client_name FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        name = (named[0] if named else None) or "remote-client"
        cur = conn.execute("INSERT INTO agent_clients(client_name,client_version,pid,cwd,project,started_at,last_seen_at,remote_client_id) VALUES (?,?,?,?,?,?,?,?)",
                           (name, None, None, None, None, now(), now(), client_id))
        return cur.lastrowid, name


# ---------------------------------------------------------------- OAuth provider

@dataclass
class Pending:
    client_id: str
    client_name: str
    redirect_uri: str
    explicit: bool
    state: str | None
    scopes: list[str]
    code_challenge: str
    resource: str | None
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires_at: float = field(default_factory=lambda: time.time() + PENDING_TTL_S)


class DevmemOAuthProvider:
    """OAuthAuthorizationServerProvider on SQLite. authorize() never grants: it parks the request behind the passphrase page."""

    def __init__(self, public_url: str):
        self.public_url = public_url.rstrip("/")
        self.pending: dict[str, Pending] = {}
        self.limiter = AttemptLimiter()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with connection() as conn:
            row = conn.execute("SELECT client_info_json FROM oauth_clients WHERE client_id=? AND revoked_at IS NULL", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        allowed, uris = set(redirect_allowlist()), [str(uri) for uri in client_info.redirect_uris or []]
        if not uris or any(uri not in allowed for uri in uris):
            raise RegistrationError("invalid_redirect_uri", "redirect_uri is not allowed")
        with connection() as conn:
            conn.execute("INSERT INTO oauth_clients(client_id,client_info_json,client_name,created_at) VALUES(?,?,?,?)",
                         (client_info.client_id, client_info.model_dump_json(), client_info.client_name, now()))

    def _prune(self) -> None:
        t = time.time()
        for key in [k for k, p in self.pending.items() if p.expires_at <= t]: del self.pending[key]
        while len(self.pending) >= MAX_PENDING: del self.pending[next(iter(self.pending))]

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._prune()
        request_id = secrets.token_urlsafe(24)
        self.pending[request_id] = Pending(
            client_id=client.client_id, client_name=client.client_name or "unnamed client", redirect_uri=str(params.redirect_uri),
            explicit=params.redirect_uri_provided_explicitly, state=params.state, scopes=params.scopes or (client.scope or SCOPE).split(),
            code_challenge=params.code_challenge, resource=params.resource)
        return f"{self.public_url}/approve?" + urlencode({"req": request_id})

    def get_pending(self, request_id: str | None) -> Pending | None:
        self._prune()
        return self.pending.get(request_id or "")

    def issue_code(self, request_id: str) -> str | None:
        """Consume an approved pending request and return the redirect carrying a fresh single-use code."""
        pending = self.pending.pop(request_id, None)
        if pending is None or pending.expires_at <= time.time(): return None
        code = secrets.token_urlsafe(32)
        with connection() as conn:
            if not conn.execute("SELECT 1 FROM oauth_clients WHERE client_id=? AND revoked_at IS NULL", (pending.client_id,)).fetchone(): return None
            conn.execute("INSERT INTO oauth_codes(code_hash,client_id,redirect_uri,code_challenge,scopes,resource,expires_at) VALUES(?,?,?,?,?,?,?)",
                         (_hash(code), pending.client_id, pending.redirect_uri if pending.explicit else None, pending.code_challenge,
                          " ".join(pending.scopes), pending.resource, int(time.time()) + CODE_TTL_S))
        return construct_redirect_uri(pending.redirect_uri, code=code, state=pending.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        with connection() as conn:
            row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash=? AND client_id=? AND used_at IS NULL",
                               (_hash(authorization_code), client.client_id)).fetchone()
        if not row: return None
        # A NULL redirect_uri means /authorize did not name one, so the client's single registered URI was used.
        redirect = row["redirect_uri"] or str((client.redirect_uris or [""])[0])
        return AuthorizationCode(code=authorization_code, scopes=row["scopes"].split(), expires_at=row["expires_at"], client_id=row["client_id"],
                                 code_challenge=row["code_challenge"], redirect_uri=redirect,
                                 redirect_uri_provided_explicitly=row["redirect_uri"] is not None, resource=row["resource"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        with connection() as conn:
            cur = conn.execute("UPDATE oauth_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL", (now(), _hash(authorization_code.code)))
            if cur.rowcount != 1: raise TokenError("invalid_grant", "authorization code is no longer valid")
            return _issue_tokens(conn, client.client_id, authorization_code.scopes, authorization_code.resource, None)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        token_hash = _hash(refresh_token)
        with connection() as conn:
            row = conn.execute("SELECT * FROM oauth_tokens WHERE token_hash=? AND kind='refresh'", (token_hash,)).fetchone()
            if not row or row["client_id"] != client.client_id: return None
            if row["revoked_at"]:
                _revoke_chain(conn, token_hash)  # a rotated-away token came back: assume it leaked
                return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"].split(), expires_at=row["expires_at"], resource=row["resource"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        token_hash = _hash(refresh_token.token)
        with connection() as conn:
            cur = conn.execute("UPDATE oauth_tokens SET revoked_at=? WHERE token_hash=? AND kind='refresh' AND revoked_at IS NULL", (now(), token_hash))
            if cur.rowcount != 1:  # lost a race with another use of the same token
                _revoke_chain(conn, token_hash); conn.commit()
                raise TokenError("invalid_grant", "refresh token is no longer valid")
            return _issue_tokens(conn, client.client_id, scopes, refresh_token.resource, token_hash)

    async def load_access_token(self, token: str) -> AccessToken | None:
        with connection() as conn:
            row = conn.execute("""SELECT t.* FROM oauth_tokens t JOIN oauth_clients c ON c.client_id=t.client_id
                WHERE t.token_hash=? AND t.kind='access' AND t.revoked_at IS NULL AND c.revoked_at IS NULL AND t.expires_at>?""",
                               (_hash(token), int(time.time()))).fetchone()
            if not row: return None
            conn.execute("UPDATE oauth_clients SET last_used_at=? WHERE client_id=?", (now(), row["client_id"]))
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(), expires_at=row["expires_at"], resource=row["resource"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke the token and its pair: an access token's refresh token, or a refresh token's access tokens."""
        token_hash = _hash(token.token)
        with connection() as conn:
            row = conn.execute("SELECT kind,parent_hash FROM oauth_tokens WHERE token_hash=?", (token_hash,)).fetchone()
            if not row: return
            pair = row["parent_hash"] if row["kind"] == "access" else token_hash
            conn.execute("""UPDATE oauth_tokens SET revoked_at=? WHERE revoked_at IS NULL
                AND (token_hash=? OR token_hash=? OR (kind='access' AND parent_hash=?))""", (now(), token_hash, pair, pair))


# ---------------------------------------------------------------- approval page

_STATIC = Path(__file__).parent / "panel" / "static"
_TEMPLATES = Path(__file__).parent / "panel" / "templates"


@functools.lru_cache(maxsize=1)
def _styles() -> Markup:
    """The panel's own tokens.css, base.css and site.css, inlined: mcp-http serves no static files over the Funnel."""
    parts = [(_STATIC / name).read_text(encoding="utf-8") for name in ("tokens.css", "base.css", "site.css")]
    text = "\n".join(line for part in parts for line in part.splitlines() if not line.lstrip().startswith("@import"))
    return Markup(text.replace("</", "<\\/"))


@functools.lru_cache(maxsize=1)
def _template():
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    return Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=select_autoescape(["html"])).get_template("remote_approve.html")


_PAGE_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"}


def _page(status: int, *, pending: Pending | None = None, request_id: str = "", error: str | None = None):
    from starlette.responses import HTMLResponse
    context = {"styles": _styles(), "error": error, "pending": pending, "request_id": request_id,
               "redirect_host": urlparse(pending.redirect_uri).hostname if pending else None}
    return HTMLResponse(_template().render(**context), status_code=status, headers=_PAGE_HEADERS)


def approval_routes(provider: DevmemOAuthProvider):
    """GET shows the pending request; POST checks CSRF, the attempt limiter and the passphrase, then redirects with a code."""
    import anyio
    from starlette.responses import RedirectResponse

    async def approve(request):
        if request.method == "GET":
            request_id = request.query_params.get("req", "")
            pending = provider.get_pending(request_id)
            if pending is None: return _page(400, error="This sign-in request is unknown or has expired. Start again from the client.")
            return _page(200, pending=pending, request_id=request_id)
        form = await request.form()
        request_id, csrf, passphrase = (form.get(name) for name in ("req", "csrf", "passphrase"))
        pending = provider.get_pending(request_id if isinstance(request_id, str) else None)
        if pending is None or not isinstance(csrf, str) or not hmac.compare_digest(csrf.encode(), pending.csrf.encode()):
            return _page(400, error="This sign-in request is unknown or has expired. Start again from the client.")
        ip = source_ip(request)
        if provider.limiter.blocked(ip):
            return _page(429, pending=pending, request_id=request_id, error="Too many attempts. Try again later.")
        try: stored = load_config().get("passphrase_scrypt")
        except ValueError: stored = None
        ok = isinstance(passphrase, str) and await anyio.to_thread.run_sync(check_passphrase, passphrase, stored)
        if not ok:
            provider.limiter.failed(ip)
            return _page(401, pending=pending, request_id=request_id, error="Not approved.")
        target = provider.issue_code(request_id)
        if target is None: return _page(400, error="This sign-in request is unknown or has expired. Start again from the client.")
        return RedirectResponse(target, 303, headers={"Cache-Control": "no-store"})

    return approve


# ---------------------------------------------------------------- remote FastMCP

def _identity(ctx) -> tuple[int, str]:
    """The calling OAuth client, from this HTTP request's authenticated user (falling back to the SDK's auth contextvar)."""
    try: user = getattr(ctx.request_context.request, "scope", {}).get("user")
    except Exception: user = None
    token = user.access_token if isinstance(user, AuthenticatedUser) else get_access_token()
    if token is None: raise PermissionError("remote tool call without an access token")
    return agent_client(token.client_id)


def _bind(fn):
    """Run an existing stdio tool with the caller's identity in mcp_server._remote_client; signature and docs stay the tool's own."""
    from . import mcp_server
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def tool(*args, **kwargs):
            token = mcp_server._remote_client.set(_identity(kwargs.get("ctx")))
            try: return await fn(*args, **kwargs)
            finally: mcp_server._remote_client.reset(token)
    else:
        @functools.wraps(fn)
        def tool(*args, **kwargs):
            token = mcp_server._remote_client.set(_identity(kwargs.get("ctx")))
            try: return fn(*args, **kwargs)
            finally: mcp_server._remote_client.reset(token)
    return tool


def build(cfg: dict[str, Any], provider: DevmemOAuthProvider | None = None):
    """The remote FastMCP instance: SDK OAuth server, the allowed tool subset, and the /approve page."""
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from . import mcp_server

    public = normalise_url(cfg["public_url"])
    provider = provider or DevmemOAuthProvider(public)
    parsed = urlparse(public)
    hosts = [parsed.netloc, parsed.hostname, "127.0.0.1:*", "localhost:*"]
    server = FastMCP(
        "devmem", instructions=mcp_server.INSTRUCTIONS + "\n" + REMOTE_NOTE, auth_server_provider=provider, host="127.0.0.1",
        auth=AuthSettings(issuer_url=public, resource_server_url=public + "/mcp",
                          client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
                          revocation_options=RevocationOptions(enabled=True), required_scopes=[SCOPE]),
        # Funnel forwards the public Host header, which the SDK's loopback default would refuse.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=list(dict.fromkeys(hosts)),
                                                     allowed_origins=[public, "https://claude.ai", "https://claude.com"]))
    for name in REMOTE_TOOLS:
        local = mcp_server.mcp._tool_manager.get_tool(name)
        server.add_tool(_bind(local.fn), name=name, title=local.title, description=local.description, annotations=local.annotations)
    server.custom_route("/approve", methods=["GET", "POST"])(approval_routes(provider))
    return server


class _NoQueryString(logging.Filter):
    """Access-log lines keep the path only: OAuth query strings carry state and codes."""
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def run(port: int = DEFAULT_PORT) -> None:
    try: cfg = load_config()
    except ValueError as exc: raise SystemExit(f"devmem mcp-http: {exc}")
    problem = setup_problem(cfg)
    if problem:
        raise SystemExit(f"devmem mcp-http: {problem}; run `devmem remote setup --url https://<machine>.<tailnet>.ts.net:8443` first.")
    import uvicorn
    app = build(cfg).streamable_http_app()
    # proxy_headers=False: the attempt limiter reads X-Forwarded-For itself, and only from 127.0.0.1.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, proxy_headers=False, timeout_graceful_shutdown=2)
    logging.getLogger("uvicorn.access").addFilter(_NoQueryString())
    uvicorn.Server(config).run()
