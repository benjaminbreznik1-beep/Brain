# devmem

Local, single-user long-term coding memory for MCP clients, plus a localhost usage and memory dashboard. It makes no network calls and keeps its SQLite database at `~/.devmem/devmem.db` (override with `DEVMEM_DB`).

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

Run `devmem panel` and visit `http://127.0.0.1:8765`. The panel listens only on localhost and imports usage logs every 60 seconds. Run `devmem ingest` for a one-off import, or `devmem search "query"` from a shell.

Claude logs are read from `~/.claude/projects`; Codex logs from `~/.codex/sessions`. Logs are never changed. Token figures are from local logs; Claude subscription limits are not available there.
