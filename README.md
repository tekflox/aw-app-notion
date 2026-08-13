# Notion

Notion connects an AW Workspace to a Notion internal integration. It lets agents use Notion pages, databases, comments, and search after the workspace has been given a valid Notion token — and turns one designated Notion database into the **Agents Kanban board**.

## What It Does

- Stores a Notion integration token through workspace-managed secrets.
- Adds Notion tools for agents, including search, page retrieval, page updates, database access, and comments.
- Adds the **`aw-kanban` tools** — list, create, move and comment on Kanban cards, set QA verdicts, flag blockers.
- Adds `aw-workspace-cli notion-sync` — pulls Notion notes into the knowledge base and rebuilds its index.
- Provides a settings window for connecting the workspace to Notion.
- Works with the workspace tool gateway so Notion access is available from agent sessions.

## Why Use It

Use this app when workspace agents need to read or update Notion content. It is useful for project notes, task databases, meeting pages, documentation spaces, and any workflow where Notion is part of the source of truth. If your team tracks agent work on a Notion board, the Kanban half gives agents first-class access to it instead of raw page edits.

## How To Use It

Create a Notion internal integration, copy its token, install this app, and save the token in the app settings. In Notion, share each page or database with that integration. After that, agents can use the Notion tools from the workspace.

For the Kanban board, additionally set `kanban_database_id` in the app's config to the board's database id, and share that database with the integration. The `aw-kanban` skill (`skills/aw-kanban/SKILL.md`) is the agent-facing reference.

For the knowledge-base sync, set `sync_root_page_id` to the Notion page whose child pages should be mirrored, and `sync_bidirectional` if deletions should propagate back.

## `aw-workspace-cli notion-sync`

Replaces the monolith's `./aw notion-sync`. Every child page under `sync_root_page_id` is converted to Markdown and written to `<AW_WORKSPACE_HOME>/knowledge_base/notes/<slug>.md` — the tree aw-app-kb indexes — then the KB is reindexed.

```bash
aw-workspace-cli notion-sync                     # changed pages only
aw-workspace-cli notion-sync --force             # re-sync everything
aw-workspace-cli notion-sync --bidirectional     # also push deletions
aw-workspace-cli notion-sync --no-rebuild        # write notes, skip the reindex
aw-workspace-cli notion-sync --status            # what the last sync did
```

The command is a thin client over `POST /api/apps/notion/sync`, not a local reimplementation: the Notion token lives in this app's secret store, readable only by the app inside the workspace process. A failed KB reindex exits 2 — the notes are still written, and the next build picks them up.

## Two MCP servers, one app

The generated `mcp.json` advertises both:

| Server | What it is |
|---|---|
| `notion` | The official upstream `@notionhq/notion-mcp-server`, spawned by MCP Gateway as an `npx` subprocess. Generic Notion access. |
| `aw-kanban` | This app's own tools, served in-process over Streamable HTTP at `/api/apps/notion/mcp`. Board-specific. |

Neither is advertised without a token — they appear and disappear together.

## Ported from agentic-workspace — and what was left behind

The Kanban half comes from the monolith's `src/api/routes/notion_kanban.py`, `src/api/kanban_manager.py` and `src/mcp/kanban.py`. Only the part that is genuinely "a Notion database used as a Kanban board" moved.

**Deliberately not ported** — these are agents-platform orchestration, not Notion:

- The `POST /api/notion/webhook` dispatcher that fired agent runs when a card hit *Ready*.
- The Telegram approval keyboard (`[▶ Executar]` / `[⏭ Pular]`) and the `start_now` shortcut.
- `invoke_kanban_agent`, `run_ready_cards`.
- The `**agent_slug** — [run_id](url)` comment byline, which was resolved by querying agents-platform's `/api/runs`.
- Stale-card auto-archival, which piggybacked on Notion webhook traffic that doesn't exist here.

So `create_kanban_task` creates the card and stops, and `move_kanban_task(status="ready")` sets the status without dispatching anything. Both say so in their responses rather than quietly doing less than the name implies.

**Not ported yet:** `attach_kanban_file` / `attach_kanban_presentation`. Attaching a real image/PDF block needs Notion's multipart file-upload API, which the monolith reached through an awserv route that proxied the bytes.

## What It Delivers

The app gives AW Workspace controlled Notion access. Users decide what the integration can see, and agents can work with that shared Notion content — including a real Kanban board — when tasks require it.
