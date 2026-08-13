# Notion

Notion connects an AW Workspace to a Notion internal integration. It lets agents use Notion pages, databases, comments, and search after the workspace has been given a valid Notion token — and turns one designated Notion database into the **Agents Kanban board**.

## What It Does

- Stores a Notion integration token through workspace-managed secrets.
- Adds Notion tools for agents, including search, page retrieval, page updates, database access, and comments.
- Adds the **`aw-kanban` tools** — list, create, move and comment on Kanban cards, set QA verdicts, flag blockers.
- Adds `aw-workspace-cli notion-sync` — mirrors Notion notes *and* the Kanban board into the knowledge base, then rebuilds its index.
- Provides a settings window for connecting the workspace to Notion.
- Works with the workspace tool gateway so Notion access is available from agent sessions.

## Why Use It

Use this app when workspace agents need to read or update Notion content. It is useful for project notes, task databases, meeting pages, documentation spaces, and any workflow where Notion is part of the source of truth. If your team tracks agent work on a Notion board, the Kanban half gives agents first-class access to it instead of raw page edits.

## How To Use It

Create a Notion internal integration, copy its token, install this app, and save the token in the app settings. In Notion, share each page or database with that integration. After that, agents can use the Notion tools from the workspace.

For the Kanban board, additionally set `kanban_database_id` in the app's config to the board's database id, and share that database with the integration. The `aw-kanban` skill (`skills/aw-kanban/SKILL.md`) is the agent-facing reference.

For the knowledge-base sync, set `sync_root_page_id` to the Notion page whose child pages should be mirrored, and `sync_bidirectional` if deletions should propagate back.

## `aw-workspace-cli notion-sync`

Replaces the monolith's `./aw notion-sync`, and does more than it did. Everything is mirrored under one `notion/` root inside the KB tree (`<AW_WORKSPACE_HOME>/knowledge_base/`), so a search result's path says where it came from:

```
notion/
  notes/                     ← child pages under sync_root_page_id
  kanban/
    backlog/                 ← one dir per status, keyed on the LOGICAL status
    ready/
    done/
    need_human/
    ...
```

```bash
aw-workspace-cli notion-sync                     # both halves, changed only
aw-workspace-cli notion-sync --force             # re-render everything
aw-workspace-cli notion-sync --notes-only
aw-workspace-cli notion-sync --kanban-only
aw-workspace-cli notion-sync --bidirectional     # notes: also push deletions
aw-workspace-cli notion-sync --no-rebuild        # write files, skip the reindex
aw-workspace-cli notion-sync --status            # what the last sync did
```

The command is a thin client over `POST /api/apps/notion/sync`, not a local reimplementation: the Notion token lives in this app's secret store, readable only by the app inside the workspace process. A failed KB reindex exits 2 — the files are still written, and the next build picks them up.

### The two mirrors are not symmetric

`notion/notes/` is a **sync**. With `sync_bidirectional` on, deleting a note archives its Notion page.

`notion/kanban/` is a **derived mirror**. Deleting a file there does nothing to Notion — the card is re-exported on the next run. A Kanban card's lifecycle belongs to the board, and the MCP tools (`move_kanban_task`, …) are how you change it.

Status dirs are keyed on the *logical* status (`need_human`), not the Notion label (`Need Human`), so renaming an option in Notion doesn't move every file. "Auto-resolvido" → "Self-closed" already happened once. A card that changes status has its old file removed, and a status dir left empty is pruned — a stale empty `ready/` would read as "nothing is ready".

Cards sync incrementally off `last_edited_time`, which comes back on the board query itself, so an unchanged card costs zero extra Notion calls. One caveat: posting a Notion comment does *not* bump `last_edited_time`, so a comment-only change is picked up by `--force`, not by an incremental run.

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
