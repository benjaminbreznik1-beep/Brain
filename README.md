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

Run `devmem panel` and visit `http://127.0.0.1:8765`. The panel listens only on localhost and imports logs every 5 seconds (unchanged files cost one `stat`). `/` is the live brain wall (press W or use "Wall mode" to hide the navigation); `/overview`, `/projects`, `/sessions`, `/agents` and `/memory` are drill-downs. `GET /api/state` returns one JSON snapshot and `GET /api/stream` pushes it, plus `pulse` events, over Server-Sent Events. Run `devmem ingest` for a one-off import, or `devmem search "query"` from a shell.

Claude logs are read from `~/.claude/projects`; Codex logs from `~/.codex/sessions`. Logs are never changed. Token figures and tool activity (tool name plus a short hint such as a description or file path, never commands, file contents or prompts) come from local logs. Claude subscription limits are read from `~/.devmem/claude_limits.json`, which a Claude Code status-line script keeps current (override with `DEVMEM_CLAUDE_LIMITS`); limit rows are stored only when values change. The brain page loads d3 7.9.0 from cdnjs; without it the network falls back to a static layout.
