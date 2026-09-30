---
name: aw-notion
description: >-
  How to use the Notion MCP tools exposed through MCP Gateway once the
  aw-app-notion app is installed and a token is saved. Covers tool names
  (API-query-data-source, API-retrieve-page-markdown, API-update-page-markdown,
  API-move-page, API-post-search, comments, ...), the mcp-gateway tool-name prefix, page
  vs data-source IDs, and what has to be shared with the Notion integration
  before any tool call can see it. Load this whenever a task needs to
  read/write a Notion page or database. NOT the same as aw-kanban (that
  skill wraps agentic-workspace's own bespoke Kanban board via awserv, a
  different, unrelated MCP) — this is the generic, official
  @notionhq/notion-mcp-server, usable against any Notion workspace.
---

# aw-notion — generic Notion MCP integration

This app ports agentic-workspace's `notion` MCP entry (`src/config/mcp.json`)
into a decoupled aw-workspace app: it stores a Notion internal-integration
token in the zero-knowledge secret store and generates the `mcp.json` file
MCP Gateway scans, spawning `npx -y @notionhq/notion-mcp-server` as a stdio
upstream named `notion`. Once loaded, its tools show up through the gateway
prefixed the same way every other upstream is (see `aw-kanban/SKILL.md` for
the general prefix convention if unfamiliar):

```
mcp__aw-gateway__aw__notion__API-<tool-name>
```

## Before calling anything: is there a token, and can it see the page?

Two independent prerequisites, both set up from the app's own settings
window (Notion app card → open it):

1. **A token is saved.** `GET /api/apps/notion/status` → `configured: true`.
   No token means MCP Gateway has no `notion` upstream at all — tool calls
   will fail as "unknown tool" / "unknown server", not as a Notion API
   error.
2. **The integration can see the target page/database.** A Notion internal
   integration has NO access to anything by default, even with a valid
   token — each page or database must be explicitly shared with it
   (`···` menu → **Connect to integration**, or bulk via the integration's
   own **Access** tab at notion.so/profile/integrations). A tool call
   against an unshared page returns a 404-shaped "not found" from Notion's
   API even though the page exists — that's the standard symptom, not a
   bug in this app.

If a token was just saved, MCP Gateway may still need a manual pick-up —
see this app's README ("no automatic hot-reload for the token" — a
deliberate trade-off to avoid ever routing the token through aw-workspace's
plain, potentially cloud-synced app config, same reasoning as aw-app-git's
`github_token`). Reinstall/restart the **MCP Gateway** app if
`aw__notion__API-post-search` (or any other `aw__notion__API-*` tool) isn't
showing up yet after a fresh save.

## Tool surface (v2.0.0 of @notionhq/notion-mcp-server)

Confirmed tool names, from the upstream project's own docs — this is NOT
the full list (the package ships 24 tools total; the ones below are the
ones documented by name upstream as of this port). When in doubt, list the
gateway's actual `tools/list` output rather than trusting this table blind
— the upstream package can add/rename tools between versions.

| Tool | Use it to |
|---|---|
| `API-post-search` | Find pages/databases by title/content across the workspace (only what's shared with the integration). Start here when you don't already have a page/database ID. |
| `API-retrieve-page-markdown` | Read a page's content as Markdown — the cheap way to read, no block-tree parsing needed. |
| `API-update-page-markdown` | Edit a page's content via Markdown. |
| `API-move-page` | Relocate a page to a different parent page/database. |
| `API-retrieve-a-database` | Get a database's metadata, including its data source IDs (a database's actual rows now live under one or more "data sources" — 2025 API model, not the old flat-database shape). |
| `API-retrieve-a-data-source` / `API-update-a-data-source` / `API-create-a-data-source` | Read/modify/create a database's schema (properties, not rows). |
| `API-query-data-source` | Query rows with filters/sorts — the equivalent of the old "query a database" call. |
| `API-list-data-source-templates` | See templates available for a data source. |
| `API-create-a-comment` / `API-retrieve-a-comment` | Post/read comments on a page. |

## Attaching a real file (PDF, image, …) to a page

None of the upstream `API-*` tools above can upload bytes — Notion's own
`@notionhq/notion-mcp-server` has no file-upload tool. That gap is filled by
`upload_file_to_page`, served from this app's **other** MCP server
(`aw-kanban`, over Streamable HTTP — see `aw-kanban/SKILL.md` for the prefix
convention), reachable as
`mcp__aw-gateway__aw__aw_kanban__upload_file_to_page`. Don't be misled by the
server name: despite living next to the Kanban tools, it works against **any**
Notion page the integration can see, not just a Kanban card.

```
upload_file_to_page(page_id, file_path, caption=None)
```

Runs Notion's File Upload API in one call — create an upload slot
(`POST /v1/file_uploads`), send the bytes (`POST /v1/file_uploads/{id}/send`,
multipart/form-data), then append a `file`/`image`/`pdf` block referencing it
— because Notion deletes an upload that isn't attached to something within an
hour, so the three steps can't be split across separate tool calls. `file_path`
is read off the **aw-workspace filesystem** (write it to `.tmp/` first if it
came from somewhere else), and the same 20 MB single-part ceiling as
`attach_kanban_file` applies. Same prerequisite as everything else here: the
target page must already be shared with the integration, or the call fails
with `object_not_found`.

## IDs

Notion page/database/data-source IDs are UUIDs, usually visible in the
page's URL (the last dash-stripped 32-hex-char segment) or returned by
`API-post-search`. `API-retrieve-a-database` is the bridge from a database ID
(what a URL gives you) to its data source ID (what `API-query-data-source`
wants).

## Comparison: this vs. aw-kanban

`aw-kanban` (agentic-workspace's own skill/MCP) is a **different, unrelated
MCP server** — a bespoke wrapper around awserv's REST surface for one
specific Notion database (the dev/QA Kanban board), with domain tools like
`move_kanban_task`/`set_qa_status` that know about this workspace's own
card schema. It was NOT ported here. `aw-notion` (this skill) is the
generic, official Notion MCP — arbitrary read/write against whatever pages
a token's integration has been shared with, no board-specific assumptions.
Reach for `aw-kanban` only if you're specifically instrumenting
agentic-workspace's own dev workflow; reach for this for everything else
Notion-shaped.
