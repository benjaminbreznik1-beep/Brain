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

## Automatic maintenance

`devmem maintain [--project DIR] [--dry-run] [--limit N]` checks stale notes and todos against local git/filesystem evidence and can update, merge, or supersede them. `--dry-run` lists and records proposals but does not alter memories, links or history. The command exits 1 when the run fails. Maintenance normally uses `codex exec` (or `claude -p` if configured), never API keys. Its optional `~/.devmem/maintain.json` config (override with `DEVMEM_MAINTAIN`) defaults to:

`devmem maintenance-cleanup [--dry-run]` needs no model: it closes resolved flags and removes old maintenance bookkeeping. The Maintenance page has the same **Clean up** action; it never changes memories.

```json
{"enabled": true, "engine": "codex", "model": null, "full_every_hours": 20, "max_changes": 15, "max_shrink": 0.5, "use_transcripts": true, "protected_kinds": ["preference", "decision"]}
```

A missing file means these defaults; an unreadable file, an unknown key or a wrongly typed value turns maintenance off and records an error run, so a typo can never silently re-enable it or drop a protection. `protected_kinds` can add kinds but never remove `preference` or `decision`.

The panel's `/maintenance` page shows runs (with errors), evidence, diffs, flags, and one-click revert; its nav badge shows unseen changes and flags, and failing runs also add a line to the session briefing. Automatic changes are guarded by mechanical/transcript evidence, a second verifier pass, loss-token checks, content/secret checks, rate limits, and a recent-edit grace period. Preferences, decisions, and `no-auto` tagged memories are flag-only, including their review dates. Background runs (panel start, `SessionStart`, `SessionEnd`) write their output to `~/.devmem/maintain.log` (`DEVMEM_MAINTAIN_LOG`); only one run happens at a time (`~/.devmem/maintain.lock`, `DEVMEM_MAINTAIN_LOCK`). Engine sessions run with `DEVMEM_MAINTAIN_CHILD=1`, and devmem's hooks do nothing inside them.

To also maintain a project's memories when a Claude session ends, add this hook by hand (it is not installed automatically):

```json
{"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "devmem maintain --session-end --hook"}]}]}}
```

## Messages between agents

Agents talk through the brain with five MCP tools: `message_post(body, to='*', subject, kind, reply_to, sender)` (kinds: message, question, finding, event; `to` is an agent name, `*`, `#channel` or `user`), `message_inbox(unread_only, limit, reader)` (messages to you, to `*`, or to channels you posted in; an unread check marks them read, `unread_only=False` re-reads without marking), `message_thread`, `message_wait(thread_id, timeout_s<=60, after_id, reader)` (returns messages newer than `after_id` as soon as one lands; every result carries `last_id` to pass as the next `after_id`; without `after_id` it starts after your own newest message in the thread, else after its root, so an early reply is never missed) and `message_close`. Subagents share their parent's MCP connection, so they pass their own name as `sender`/`reader`; the panel labels such senders "self-declared". Bodies are capped at 8,000 characters, anything matching the maintenance secret patterns is rejected and never stored, each sender may post 30 messages a minute, and closed threads older than 30 days are deleted (by maintenance runs, also when maintenance is disabled, and hourly by the panel), together with the timelines of their subagents.

The log scanner also follows Claude Code subagents (`<session>/subagents/agent-<id>.jsonl` and `.meta.json`) and Codex rollouts (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`): type, task, one line per tool call (tool name plus a path, patch file or the first 80 characters of a command, with secrets redacted; never tool results or thinking), the assistant's remarks between tool calls (first 500 characters each, redacted), status and the final report. Codex reasoning and tool outputs are never stored. A Codex task carrying a handoff id joins the newest Claude chat only when that id is visibly created or passed to Bash in exactly that Claude log; otherwise it has its own `Codex · project · task` chat. An agent is finished when its sidecar says so or its last record ends the run (a `SubagentHandback` call or its result, an `end_turn` text, or Codex `task_complete`); stale after 10 quiet minutes; stopped after 2 quiet hours. Every spawn and every end (report, or "(no report: agent stopped)") becomes an `event` in the `#agents` channel, one thread per agent, closed when the agent ends. A report is never taken from a log whose last line is still being written. One unreadable log or sidecar is logged and skipped; it never stops the rest of the ingest pass.

On the Brain tab, the Messages panel (under the graph) shows the latest 20 items live, and a Claude session in the Agents panel lists its subagents. The token figures moved to Overview. The `/messages` tab is a WhatsApp-style chat view: a chat list on the left (one row per Claude Code session, per standalone `message_post` thread and per `#channel`, newest first, with preview, time and unread count; filters in a dropdown) and the conversation on the right. In a session chat the parent's task to each subagent is a bubble from Claude addressed `@<agent type>`, the subagent's remarks and its report (expandable) follow, tool calls collapse into small grey lines, and your own messages sit on the right. New bubbles arrive live; the box at the bottom posts as `user` to the agent with an open question, else to the last agent that spoke, which reaches an agent waiting in `message_wait`. Clicking an agent's name opens its own page (task, remarks, tool timeline, report); single threads keep their page with close/reopen. All panel POSTs from another origin are refused (Origin / Sec-Fetch-Site check). The nav badge counts unread questions plus open threads addressed to `user`. Suggested agent instructions are in `docs/agent-messages-instructions.md`.

Automatic replies to new panel messages are optional: set `enabled` in
`~/.devmem/dispatch.json` and restart the panel after running the tests.
Routing uses fast explicit requests (`@Codex`, `Claude, ...`, or
`please ask Codex to ...`), otherwise retaining the current agent. There is
no routing model call. A quota error or unresponsive runner gets one
fallback to the other agent, with the handoff and answer shown in the chat.
See [message-dispatch.md](docs/message-dispatch.md) for configuration and
delivery behavior.

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
