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

## Memory history and pins

Updates and deletes retain a revision history; use `memory_history` to inspect it and `memory_restore` to undo a change or recover a deleted memory. `memory_save` may return same-scope `similar` hints when embeddings are available: they never block a save, but a duplicate should be merged into the existing memory. Set `pinned=true` only for memories every session needs; pinned memories from every project appear first in the context briefing. With `DEVMEM_EMBED=0`, saves still work normally and omit similar hints.

Use `memory_link` to connect two memories with `blocks`, `depends_on`, `relates_to`, `overlaps`, `fixes`, or `supersedes`, instead of repeating the related memory's content. Use `memory_unlink` to remove one typed link or all link types between two memories.

Memory content is capped at 2,000 characters by default (`DEVMEM_MAX_CONTENT` overrides it). Put lengthy material in a file and save a concise summary with its path. For status, handoff, and in-progress memories, set `review_after` to an ISO date (often about 14 days ahead); after that date they are marked stale and omitted from the session briefing. Run `devmem audit` for a read-only report of oversized, expired, likely duplicate, old, and outdated-wording memories. Optional outdated terms live in `~/.devmem/audit.json` (or `DEVMEM_AUDIT`), for example:

```json
{"outdated_terms": ["legacy-label", "old-role"]}
```

Before any future schema upgrade of an existing database, devmem creates a consistent SQLite snapshot in `~/.devmem/backups/` (override with `DEVMEM_BACKUP_DIR`).

## Reminders

To receive lightweight reminders after relevant shell commands, add this to Claude Code's `settings.json` by hand:

```json
{"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "devmem remind --hook"}]}]}}
```

Optionally set `~/.devmem/reminders.json` (or `DEVMEM_REMINDERS`) to add your own dispatch-script patterns or other rules. A `reminders.json` replaces the defaults, so copy any defaults you want to retain: `\\bgit\\s+(switch\\s+-c|checkout\\s+-b)\\b`, `\\bgit\\s+commit\\b`, `\\bgit\\s+merge\\b`, and `\\bcodex\\s+exec\\b`.

```json
{"rules": [{"pattern": "<python regex>", "message": "<text>"}]}
```

## Azure DevOps

ADO is optional and read-only. Create a PAT with **Work Items: Read** and **Code: Read**, store it in `~/.devmem/ado_token`, then run `chmod 600 ~/.devmem/ado_token`. Also create `~/.devmem/ado.json`, a JSON object that may contain only the string keys `organization` and `project` (either may instead come from its environment variable):

```json
{"organization": "example-org", "project": "ExampleProject"}
```

Organization and project names must start with a letter or digit. Organization names otherwise use letters, digits, and hyphens (maximum 50 characters); project names otherwise use letters, digits, spaces, `.`, `_`, and `-` (maximum 64 characters). `DEVMEM_ADO_ORG` and `DEVMEM_ADO_PROJECT` override their respective file fields and must be set in the panel's environment; the MCP server reads only the panel cache. Empty environment values count as unset. `DEVMEM_ADO_CONFIG` overrides the file path and supports `~`; if it names a missing file, that is reported as an error.

The panel re-reads configuration at every refresh, so no restart is needed. ADO remains off until both a token and valid organization/project configuration exist. Broken configuration shows a safe reason on the ADO tab and through `ado_status`. When configured, the running panel polls ADO every 5 minutes. MCP clients can call `ado_status`; it reads the panel cache, so the panel must be running (set `DEVMEM_PANEL_PORT` if it is not on the default port). The ADO tab shows a `!` for changes since it was last opened; `GET /api/ado/changes` exposes the in-memory change journal.

## Network use

Network access is limited to the optional first-use embedding model download, optional five-minute ADO polling when configured, and the Brain page's CDN scripts: `3d-force-graph` plus the matching three.js bloom module. The panel otherwise stays on localhost.
