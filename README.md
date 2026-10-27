# devmem

Local, single-user long-term coding memory for MCP clients, plus a localhost usage and memory dashboard. Its SQLite database is at `~/.devmem/devmem.db` (override with `DEVMEM_DB`).

## Install

```sh
uv tool install -e .
claude mcp add devmem -s user -- devmem mcp
```

For Codex, add this to `~/.codex/config.toml`:

```toml
[mcp_servers.devmem]
command = "devmem"
args = ["mcp"]
```

Run `devmem panel` and visit `http://127.0.0.1:8765`. The panel listens only on localhost and imports logs every 5 seconds (unchanged files cost one `stat`). `/` is the live Brain wall (press W or use "Wall mode" to hide navigation); `/overview`, `/projects`, `/sessions`, `/agents`, `/memory`, and `/ado` are drill-downs. `GET /api/state` returns one JSON snapshot and `GET /api/stream` pushes it, plus `pulse` events, over Server-Sent Events. ADO is also available as `GET /api/ado`; `POST /ado/refresh` requests a live refresh. Run `devmem ingest` for a one-off import, or `devmem search "query"` from a shell.

Claude logs are read from `~/.claude/projects`; Codex logs from `~/.codex/sessions`. Logs are never changed. Token figures and tool activity (tool name plus a short hint such as a description or file path, never commands, file contents or prompts) come from local logs. Claude subscription limits are read from `~/.devmem/claude_limits.json`, which a Claude Code status-line script keeps current (override with `DEVMEM_CLAUDE_LIMITS`); limit rows are stored only when values change.

## Memory and embeddings

`memory_search` and the Memory page use hybrid keyword + semantic ranking when local embeddings are available. `devmem search "query"` uses the same hybrid search. The first embedding use may download the model to `~/.devmem/models`; run `devmem embed` to backfill existing memories in advance. Set `DEVMEM_EMBED=0` to disable embeddings entirely and use keyword results only.

Use `devmem context [--cwd DIR]` for a compact project briefing. To give Claude this briefing at session start, add this hook to its settings:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{ "type": "command", "command": "devmem context --hook" }]
    }]
  }
}
```

## Azure DevOps

ADO is optional and read-only. Create a PAT with **Work Items: Read** and **Code: Read**, store it in `~/.devmem/ado_token`, then run `chmod 600 ~/.devmem/ado_token`. The organization and project are currently hard-coded as `example-org` and `ExampleProject`. When the token exists, the running panel polls ADO every 5 minutes. MCP clients can call `ado_status`; it reads the panel cache, so the panel must be running (set `DEVMEM_PANEL_PORT` if it is not on the default port).

## Network use

Network access is limited to the optional first-use embedding model download, optional five-minute ADO polling when configured, and the Brain page's CDN scripts: `3d-force-graph` plus the matching three.js bloom module. The panel otherwise stays on localhost.
