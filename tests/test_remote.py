"""Remote access (schema v13, mcp-http OAuth, tool subset, per-client identity, panel /remote), on temp DBs and a temp remote.json only."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
import sqlite3
import stat
import warnings
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.shared.auth import OAuthClientInformationFull

from devmem import db, mcp_server, remote
from devmem.db import SCHEMA_VERSION, connect, connection, now

PUBLIC = "https://devmem.example.ts.net:8443"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSPHRASE = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _remote_env(monkeypatch):
    assert "pytest" in os.environ["DEVMEM_REMOTE"]  # conftest keeps every test off the real ~/.devmem/remote.json
    monkeypatch.setattr(mcp_server, "_client_id", None); monkeypatch.setattr(mcp_server, "_client_name", None)
    monkeypatch.setattr(remote, "_SCRYPT_N", 2 ** 10)  # fast hashes; check_passphrase reads n from the stored hash
    monkeypatch.setattr(remote, "_hosts_cache", (None, []))


def _configure(**extra):
    remote.save_config({"enabled": True, "public_url": PUBLIC, "redirect_allowlist": list(remote.DEFAULT_REDIRECTS),
                        "passphrase_scrypt": remote.hash_passphrase(PASSPHRASE), **extra})


def _build():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # the SDK's validate_token_resource notice
        return remote.build(remote.load_config())


@pytest.fixture
def server():
    _configure()
    mcp = _build()
    # Not a context manager: the OAuth routes need no MCP session manager, and /mcp is only checked up to the bearer layer.
    client = TestClient(mcp.streamable_http_app(), base_url=PUBLIC)
    return SimpleNamespace(client=client, provider=mcp._auth_server_provider, mcp=mcp)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _register(client, redirect=CALLBACK, name="Claude"):
    return client.post("/register", json={"redirect_uris": [redirect], "client_name": name, "token_endpoint_auth_method": "none",
                                          "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})


def _authorize(client, client_id, challenge, state="st-1") -> str:
    response = client.get("/authorize", params={"client_id": client_id, "redirect_uri": CALLBACK, "response_type": "code", "code_challenge": challenge,
                                               "code_challenge_method": "S256", "state": state, "scope": "devmem", "resource": PUBLIC + "/mcp"},
                          follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(PUBLIC + "/approve?req=")  # authorize() never grants by itself
    return parse_qs(urlparse(location).query)["req"][0]


def _csrf(page: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def _approve(client, request_id, passphrase, csrf=None):
    csrf = csrf or _csrf(client.get("/approve", params={"req": request_id}).text)
    return client.post("/approve", data={"req": request_id, "csrf": csrf, "passphrase": passphrase}, follow_redirects=False)


def _code_for(client, client_id, challenge, state="st-1") -> str:
    response = _approve(client, _authorize(client, client_id, challenge, state), PASSPHRASE)
    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert response.headers["location"].startswith(CALLBACK) and query["state"] == [state]
    return query["code"][0]


def _exchange(client, client_id, code, verifier):
    return client.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "client_id": client_id,
                                       "code_verifier": verifier, "resource": PUBLIC + "/mcp"})


def _refresh(client, client_id, refresh_token):
    return client.post("/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id})


def _tokens(client, name="Claude"):
    client_id = _register(client, name=name).json()["client_id"]
    verifier, challenge = _pkce()
    response = _exchange(client, client_id, _code_for(client, client_id, challenge), verifier)
    assert response.status_code == 200, response.text
    return client_id, response.json()


def _access(provider, token):
    return asyncio.run(provider.load_access_token(token))


# ---------------------------------------------------------------- OAuth flow

def test_registration_accepts_only_allowlisted_redirects(server):
    ok = _register(server.client)
    assert ok.status_code == 201 and ok.json()["client_name"] == "Claude"
    rejected = _register(server.client, redirect="https://evil.example/callback")
    assert rejected.status_code == 400 and rejected.json()["error"] == "invalid_redirect_uri"
    with connection() as conn:
        assert [r[0] for r in conn.execute("SELECT client_name FROM oauth_clients")] == ["Claude"]


def test_full_flow_approval_page_wrong_then_right_passphrase_then_pkce_token(server):
    client_id = _register(server.client).json()["client_id"]
    verifier, challenge = _pkce()
    request_id = _authorize(server.client, client_id, challenge)
    page = server.client.get("/approve", params={"req": request_id})
    assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text and "devmem" in page.text
    assert page.headers["x-frame-options"] == "DENY" and "no-store" in page.headers["cache-control"]
    assert 'href="/' not in page.text  # no panel navigation on the public page
    csrf = _csrf(page.text)
    assert server.client.post("/approve", data={"req": request_id, "csrf": "forged", "passphrase": PASSPHRASE}, follow_redirects=False).status_code == 400
    wrong = _approve(server.client, request_id, "not it", csrf)
    assert wrong.status_code == 401 and "location" not in wrong.headers and "Not approved." in wrong.text
    right = _approve(server.client, request_id, PASSPHRASE, csrf)
    assert right.status_code == 303
    code = parse_qs(urlparse(right.headers["location"]).query)["code"][0]
    assert _approve(server.client, request_id, PASSPHRASE, csrf).status_code == 400  # the pending request is consumed
    assert _exchange(server.client, client_id, code, "wrong-verifier-" + "x" * 40).json()["error"] == "invalid_grant"
    tokens = _exchange(server.client, client_id, code, verifier).json()
    assert tokens["token_type"] == "Bearer" and tokens["scope"] == "devmem" and tokens["expires_in"] == 3600
    assert _exchange(server.client, client_id, code, verifier).json()["error"] == "invalid_grant"  # single use
    access = _access(server.provider, tokens["access_token"])
    assert access.client_id == client_id and access.scopes == ["devmem"] and access.resource == PUBLIC + "/mcp"
    with connection() as conn:
        stored = [tuple(r) for r in conn.execute("SELECT * FROM oauth_tokens")] + [tuple(r) for r in conn.execute("SELECT * FROM oauth_codes")]
        assert {r[0] for r in conn.execute("SELECT token_hash FROM oauth_tokens")} == {hashlib.sha256(t.encode()).hexdigest() for t in (tokens["access_token"], tokens["refresh_token"])}
        assert conn.execute("SELECT last_used_at FROM oauth_clients").fetchone()[0] is not None
    flat = repr(stored)
    assert tokens["access_token"] not in flat and tokens["refresh_token"] not in flat and code not in flat


def test_unknown_or_expired_approval_request_is_refused(server, monkeypatch):
    assert server.client.get("/approve", params={"req": "nope"}).status_code == 400
    client_id = _register(server.client).json()["client_id"]
    request_id = _authorize(server.client, client_id, _pkce()[1])
    server.provider.pending[request_id].expires_at = 0
    assert server.client.get("/approve", params={"req": request_id}).status_code == 400


def test_expired_code_and_expired_access_token_are_refused(server):
    client_id = _register(server.client).json()["client_id"]
    verifier, challenge = _pkce()
    code = _code_for(server.client, client_id, challenge)
    with connection() as conn: conn.execute("UPDATE oauth_codes SET expires_at=1")
    assert _exchange(server.client, client_id, code, verifier).json()["error"] == "invalid_grant"
    _, tokens = _tokens(server.client)
    with connection() as conn: conn.execute("UPDATE oauth_tokens SET expires_at=1 WHERE kind='access'")
    assert _access(server.provider, tokens["access_token"]) is None
    assert server.client.post("/mcp", headers={"Authorization": "Bearer " + tokens["access_token"]}, json={}).status_code == 401


def test_refresh_rotates_and_reuse_revokes_the_whole_chain(server):
    client_id, first = _tokens(server.client)
    second = _refresh(server.client, client_id, first["refresh_token"])
    assert second.status_code == 200
    second = second.json()
    assert second["refresh_token"] != first["refresh_token"] and _access(server.provider, second["access_token"])
    third = _refresh(server.client, client_id, second["refresh_token"]).json()
    assert _access(server.provider, third["access_token"])
    # The rotated-away first refresh token comes back: everything issued from it is revoked.
    assert _refresh(server.client, client_id, first["refresh_token"]).json()["error"] == "invalid_grant"
    assert _access(server.provider, third["access_token"]) is None and _access(server.provider, first["access_token"]) is None
    assert _refresh(server.client, client_id, third["refresh_token"]).json()["error"] == "invalid_grant"


def test_reuse_detection_leaves_other_chains_of_the_client_alone(server):
    client_id, first = _tokens(server.client)
    verifier, challenge = _pkce()
    other = _exchange(server.client, client_id, _code_for(server.client, client_id, challenge), verifier).json()
    rotated = _refresh(server.client, client_id, first["refresh_token"]).json()
    _refresh(server.client, client_id, first["refresh_token"])
    assert _access(server.provider, rotated["access_token"]) is None and _access(server.provider, other["access_token"])


def test_revoke_token_endpoint_revokes_the_pair(server):
    client_id, tokens = _tokens(server.client)
    assert server.client.post("/revoke", data={"token": tokens["access_token"], "client_id": client_id, "client_secret": ""}).status_code == 200
    assert _access(server.provider, tokens["access_token"]) is None
    assert _refresh(server.client, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_revoking_a_client_revokes_its_tokens_and_registration(server):
    client_id, tokens = _tokens(server.client)
    keep_id, keep = _tokens(server.client, name="Other")
    assert remote.revoke_client(client_id) and not remote.revoke_client(client_id)
    assert _access(server.provider, tokens["access_token"]) is None and _access(server.provider, keep["access_token"])
    assert asyncio.run(server.provider.get_client(client_id)) is None
    assert server.client.post("/mcp", headers={"Authorization": "Bearer " + tokens["access_token"]}, json={}).status_code == 401
    assert _refresh(server.client, client_id, tokens["refresh_token"]).status_code == 401  # unknown client
    rows = {r["client_id"]: r for r in remote.clients()}
    assert rows[client_id]["revoked_at"] and not rows[client_id]["access_tokens"] and rows[keep_id]["access_tokens"] == 1


def test_mcp_endpoint_requires_a_bearer_token(server):
    response = server.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401 and "resource_metadata" in response.headers["www-authenticate"]
    metadata = server.client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["registration_endpoint"] == PUBLIC + "/register" and metadata["code_challenge_methods_supported"] == ["S256"]
    assert server.client.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == PUBLIC + "/mcp"


def test_passphrase_change_revokes_all_tokens(server):
    _, tokens = _tokens(server.client)
    assert remote.revoke_all_tokens() == 2 and _access(server.provider, tokens["access_token"]) is None


# ---------------------------------------------------------------- rate limit

def test_five_wrong_passphrases_lock_out_the_source_for_15_minutes(server):
    clock = [1000.0]
    server.provider.limiter.clock = lambda: clock[0]
    client_id = _register(server.client).json()["client_id"]
    request_id = _authorize(server.client, client_id, _pkce()[1])
    csrf = _csrf(server.client.get("/approve", params={"req": request_id}).text)
    for _ in range(5): assert _approve(server.client, request_id, "wrong", csrf).status_code == 401
    refused = _approve(server.client, request_id, PASSPHRASE, csrf)
    assert refused.status_code == 429 and "location" not in refused.headers
    clock[0] += 899
    assert _approve(server.client, request_id, PASSPHRASE, csrf).status_code == 429
    clock[0] += 2
    assert _approve(server.client, request_id, PASSPHRASE, csrf).status_code == 303


def test_global_limit_is_20_failures_per_hour():
    clock = [0.0]
    limiter = remote.AttemptLimiter(clock=lambda: clock[0])
    for i in range(19):
        limiter.failed(f"10.0.0.{i % 4}"); clock[0] += 60
    assert not limiter.blocked("10.9.9.9")
    limiter.failed("10.0.0.1")
    assert limiter.blocked("10.9.9.9")
    clock[0] += 901
    assert not limiter.blocked("10.9.9.9")


def test_forwarded_for_is_trusted_only_from_loopback():
    def request(peer, forwarded=None):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": forwarded} if forwarded else {})
    assert remote.source_ip(request("127.0.0.1", "spoofed, 203.0.113.7")) == "203.0.113.7"
    assert remote.source_ip(request("198.51.100.2", "203.0.113.7")) == "198.51.100.2"
    assert remote.source_ip(request("127.0.0.1")) == "127.0.0.1"


# ---------------------------------------------------------------- tools and identity

def test_remote_instance_lists_exactly_the_allowed_tools():
    _configure()
    mcp = _build()
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == set(remote.REMOTE_TOOLS)
    assert not {"memory_update", "memory_delete", "memory_restore", "memory_unlink"} & set(tools)
    local = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"memory_update", "memory_delete", "memory_restore", "memory_unlink"} <= set(local)  # stdio keeps every tool
    for name, tool in tools.items():
        assert tool.description == local[name].description and tool.inputSchema == local[name].inputSchema
    assert mcp.instructions == mcp_server.INSTRUCTIONS + "\n" + remote.REMOTE_NOTE


def _client_token(provider, name):
    info = OAuthClientInformationFull(client_id=secrets.token_hex(8), client_name=name, redirect_uris=[CALLBACK], token_endpoint_auth_method="none",
                                      grant_types=["authorization_code", "refresh_token"], response_types=["code"], scope="devmem")
    asyncio.run(provider.register_client(info))
    with connection() as conn:
        issued = remote._issue_tokens(conn, info.client_id, ["devmem"], PUBLIC + "/mcp", None)
    return info.client_id, _access(provider, issued.access_token)


def _call(mcp, token, name, args):
    async def go():
        reset = auth_context_var.set(AuthenticatedUser(token))
        try: return await mcp.call_tool(name, args)
        finally: auth_context_var.reset(reset)
    return asyncio.run(go())


def test_two_tokens_are_two_agent_clients_and_calls_are_attributed():
    _configure()
    mcp = _build()
    provider = mcp._auth_server_provider
    a_id, a_token = _client_token(provider, "Claude")
    b_id, b_token = _client_token(provider, "Phone")
    _call(mcp, a_token, "message_post", {"body": "from the web", "to": "user"})
    _call(mcp, b_token, "memory_save", {"content": "saved from the phone", "scope": "global"})
    _call(mcp, a_token, "memory_search", {"query": "phone", "scope": "all"})
    with connection() as conn:
        thread = conn.execute("SELECT thread_id FROM messages").fetchone()[0]
    _call(mcp, b_token, "message_wait", {"thread_id": thread, "timeout_s": 0})
    with connection() as conn:
        rows = {r["remote_client_id"]: dict(r) for r in conn.execute("SELECT * FROM agent_clients")}
        assert set(rows) == {a_id, b_id}
        assert rows[a_id]["client_name"] == "Claude" and rows[b_id]["client_name"] == "Phone"
        assert all(r["pid"] is None and r["cwd"] is None and r["project"] is None for r in rows.values())
        calls = [(r[0], r[1]) for r in conn.execute("SELECT a.remote_client_id,t.tool FROM tool_calls t JOIN agent_clients a ON a.id=t.client_id ORDER BY t.id")]
        assert calls == [(a_id, "message_post"), (b_id, "memory_save"), (a_id, "memory_search"), (b_id, "message_wait")]
        message = conn.execute("SELECT sender,sender_kind,project FROM messages").fetchone()
        assert tuple(message) == ("Claude", "claude", None)
        assert tuple(conn.execute("SELECT source_agent,project FROM memories").fetchone()) == ("Phone", None)
    assert mcp_server._client_id is None and mcp_server._client_name is None  # the stdio identity is untouched
    _call(mcp, a_token, "memory_list", {"scope": "all"})
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_clients").fetchone()[0] == 2  # the row is reused across calls


def test_remote_calls_keep_secret_checks_and_size_caps():
    _configure()
    mcp = _build()
    _, token = _client_token(mcp._auth_server_provider, "Claude")
    with pytest.raises(Exception, match="limit"):
        _call(mcp, token, "memory_save", {"content": "x" * (db.max_content() + 1), "scope": "global"})
    with pytest.raises(Exception):
        _call(mcp, token, "message_post", {"body": "key ghp_" + "Q" * 36, "to": "user"})
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert [r[0] for r in conn.execute("SELECT ok FROM tool_calls")] == [0, 0]


def test_mcp_http_refuses_to_start_without_setup():
    with pytest.raises(SystemExit, match="remote setup"):
        remote.run(0)
    remote.save_config({"enabled": True, "public_url": PUBLIC})
    with pytest.raises(SystemExit, match="no passphrase"):
        remote.run(0)


# ---------------------------------------------------------------- config and CLI

def test_setup_writes_a_0600_config_with_only_a_scrypt_hash(monkeypatch, capsys):
    from devmem import cli
    answers = iter([PASSPHRASE, PASSPHRASE])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    monkeypatch.setattr("sys.argv", ["devmem", "remote", "setup", "--url", PUBLIC + "/"])
    cli.main()
    path = remote.config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cfg = remote.load_config()
    assert cfg["public_url"] == PUBLIC and cfg["enabled"] and cfg["panel_hosts"] == ["devmem.example.ts.net"]
    assert cfg["redirect_allowlist"] == list(remote.DEFAULT_REDIRECTS)
    assert PASSPHRASE not in path.read_text() and remote.check_passphrase(PASSPHRASE, cfg["passphrase_scrypt"])
    assert not remote.check_passphrase("other", cfg["passphrase_scrypt"])
    assert PASSPHRASE not in capsys.readouterr().out


# ---------------------------------------------------------------- panel

def _panel(host="127.0.0.1", scheme="http"):
    from devmem.panel import app as panel
    panel._cache.value = None
    return TestClient(panel.app, base_url=f"{scheme}://{host}")  # not a context manager: no background ingest


def test_remote_page_renders_and_revoke_works(monkeypatch, server):
    monkeypatch.setattr(remote, "mcp_http_running", lambda port=None: True)
    client_id, tokens = _tokens(server.client)
    _call(server.mcp, _access(server.provider, tokens["access_token"]), "project_context", {})
    page = _panel().get("/remote")
    assert page.status_code == 200 and 'href="/remote"' in page.text
    assert PUBLIC in page.text and "running" in page.text and client_id in page.text and "project_context" in page.text
    assert tokens["access_token"] not in page.text
    agents = _panel().get("/agents").text
    assert "Claude (remote)" in agents
    revoked = _panel().post(f"/remote/revoke/{client_id}", follow_redirects=False)
    assert revoked.status_code == 303 and revoked.headers["location"] == "/remote"
    assert _access(server.provider, tokens["access_token"]) is None
    assert _panel().post(f"/remote/revoke/{client_id}").status_code == 404
    cross = _panel().post(f"/remote/revoke/{client_id}", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403


def test_remote_page_when_not_set_up(monkeypatch):
    monkeypatch.setattr(remote, "mcp_http_running", lambda port=None: False)
    page = _panel().get("/remote")
    assert page.status_code == 200 and "not set up" in page.text and "not running" in page.text


def test_remote_client_shows_on_the_brain_wall():
    from devmem.panel import state
    _configure()
    mcp = _build()
    _, token = _client_token(mcp._auth_server_provider, "Claude")
    _call(mcp, token, "project_context", {})
    with connection() as conn:
        connected = state.connected_clients(conn)
    assert [(c["client_name"], c["remote"], c["family"]) for c in connected] == [("Claude (remote)", True, "claude")]


def test_extra_panel_host_is_allowed_only_when_configured():
    assert _panel("box.example.ts.net", "https").get("/agents").status_code == 400
    _configure(panel_hosts=["box.example.ts.net"])
    assert _panel("box.example.ts.net", "https").get("/agents").status_code == 200
    assert _panel("other.example.ts.net", "https").get("/agents").status_code == 400
    assert _panel().get("/agents").status_code == 200


# ---------------------------------------------------------------- migration

def _noop(conn): pass


def test_v12_database_migrates_to_v13_with_backup(tmp_path, monkeypatch):
    path = tmp_path / "v12.db"
    with monkeypatch.context() as m:
        m.setattr(db, "SCHEMA_VERSION", 12)
        for step in range(13, SCHEMA_VERSION + 1): m.setattr(db, f"_migrate_v{step}", _noop)
        conn = connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 12
    assert "remote_client_id" not in {r[1] for r in conn.execute("PRAGMA table_info(agent_clients)")}
    conn.execute("INSERT INTO agent_clients(client_name,pid,started_at,last_seen_at) VALUES('claude-code',1,?,?)", (now(), now()))
    conn.commit(); conn.close()
    migrated = connect(path)
    assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION == 13
    assert {"oauth_clients", "oauth_codes", "oauth_tokens"} <= {r[0] for r in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tuple(migrated.execute("SELECT client_name,remote_client_id FROM agent_clients").fetchone()) == ("claude-code", None)
    backup = sqlite3.connect(next((tmp_path / "backups").glob("devmem-before-v13-from-v12-*.db")))
    assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == 12
    assert "oauth_tokens" not in {r[0] for r in backup.execute("SELECT name FROM sqlite_master")}
    assert not list((tmp_path / "backups").glob("*.tmp"))
    migrated.close()
    connect(path).close()  # idempotent
