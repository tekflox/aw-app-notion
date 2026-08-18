---
name: aw-kanban
description: The Agents Kanban board — a Notion database driven through the aw-kanban MCP server contributed by aw-app-notion. List, create, move and comment on cards, set QA verdicts, flag blockers. Load this whenever a run has a NOTION_TASK_ID, when asked to look at the board, or when creating/moving/commenting on a Kanban card. Ported from agentic-workspace's own aw-kanban skill; read "What did NOT come across" before assuming a monolith behaviour still exists.
---

# aw-kanban — the Agents Kanban board

The `aw-kanban` MCP server is contributed by **aw-app-notion**. It talks to
`api.notion.com` directly with the app's stored token — there is no awserv
hop and no second API key, unlike the monolith's version of this server.

Through the gateway the tools are named
`mcp__aw-gateway__aw__aw_kanban__<tool>` (or
`mcp__workspace-gateway__aw__aw_kanban__<tool>`, depending on how the gateway
is mounted in your session).

## Before anything works

Two things must be true, and they fail differently:

| Symptom | Cause | Fix |
|---|---|---|
| No `aw_kanban` tools exist at all | No Notion token saved | `POST /api/apps/notion/settings {"notion_token": "ntn_…"}`, then reload MCP Gateway |
| Tools exist, every call returns "kanban_database_id is not configured" | Board id unset | `POST /api/apps/notion/config {"kanban_database_id": "…"}` |
| Tools exist, calls return Notion `object_not_found` | The integration was never shared with the database | Notion UI → the board → ⋯ → Connect to → your integration |

That third one is the most common and the least obvious: a valid token
grants access to *nothing* by itself. `object_not_found` from Notion almost
never means the id is wrong.

## Call the tool directly — never hand-roll curl to the gateway

Once `ToolSearch` loads a tool, call it with the normal tool-call mechanism.
Do **not** write a Bash/curl workaround that POSTs to the MCP gateway
"REST API" — there is no such thing. The gateway speaks MCP JSON-RPC through
a single `/mcp` endpoint; it has no `/v1/<tool_name>` route. A guessed URL
can only 404.

If a `ToolSearch` result looks empty or ambiguous, the tool is still loaded —
the schema is attached out-of-band, so empty `content` is not a failure
signal. Just call the tool.

(The app *also* exposes a plain REST mirror at `/api/apps/notion/kanban/*`
for humans, curl and tests. That is a different thing from the gateway, and
it is not the path agents should use.)

## `page_id` is auto-filled — just omit it

Every tool takes `page_id` as optional. On a run tied to a Kanban card it
targets the right card automatically, sourced from `NOTION_TASK_ID` and
injected by the gateway as `_aw_context`. Pass `page_id` explicitly only when
you deliberately want a *different* card.

Need a page_id you don't have? `list_kanban_cards` — that's what it's for.

## Tool reference

| Tool | Use it to |
|---|---|
| `list_kanban_cards` | List the board, newest first. `status` (logical key or Notion option name) filters; omit it for every card. Also `source`, `limit` (≤100), `order` (`created`/`edited`). **New — the monolith had no read tool at all.** |
| `get_kanban_card` | One card's summary: title, status, priority, slugs, tags, timestamps, URL. |
| `create_kanban_task` | Create a finding card in Backlog. Dedupes by `finding_key`: an existing card is bumped (OccurrenceCount +1, `🔁 Re-detectado` callout) and a card already in Done is reopened as a **regression**. |
| `move_kanban_task` | Move a card's Status. `comment` is posted *before* the move. `need_human` **requires** a comment. |
| `add_kanban_comment` | Comment without changing status — progress notes, questions, delivery reports. `[label](url)` becomes a real Notion link; long text is chunked to Notion's 2000-char limit. |
| `set_kanban_property` | Set ANY property by name. The Notion type (checkbox, select, status, rich_text, number, date, multi_select, url, title) is looked up from the live schema — pass a plain value. `value=null` clears it. An unknown name fails with the list of properties that do exist. |
| `get_kanban_properties` | Read one or more (default: all) properties as plain values. |
| `set_qa_status` | QA-only, MANDATORY once at the end of every review: `done` / `ready_to_deploy` / `need_human`. Stamps `QAStatus` and moves the card. With no card for this run it's a no-op on Notion but still records the verdict — call it every time. |
| `set_blocker` | The moment you're stuck (missing tool, missing access, ambiguous ask): posts a `🚧 Blocker` comment and moves to `need_human`. Don't burn retries hunting for a workaround first. |

## Status keys

Logical keys map to Notion option names via the app's `kanban_statuses`
config (defaults ported from the monolith):

`backlog` → Backlog · `planned` → Planned · `ready` → Ready ·
`running` → In Progress · `ready_to_deploy` → Ready to Deploy ·
`need_human` → Need Human · `done` → Done · `auto_resolved` → **Self-closed**
· `done_archived` → Done Archived · `self_closed_archived` → Self-closed
Archived · `archived` → Archived

A raw Notion option name is accepted too — unknown keys pass through
unchanged rather than being rejected.

`auto_resolved` displaying as "Self-closed" is a 2026-07-14 rename in Notion
only; the logical key is unchanged so existing prompts keep working. There is
no "Ready to Test" status — it was removed the same day.

## What did NOT come across from the monolith

This is the part to read before assuming a behaviour still exists. The
monolith's Kanban was half Notion and half agents-platform orchestration.
Only the Notion half is here.

**Gone, by design — these were never about Notion:**

- **No Telegram approval.** `create_kanban_task` used to create the card
  *and* send a [▶ Executar]/[⏭ Pular] approval message. Here it creates the
  card and stops; the response says `approval_sent: false` explicitly rather
  than quietly doing less than the name promises.
- **No `start_now`, no run dispatch.** Moving a card to `ready` sets the
  status. It does not fire an agent run. Nothing in this app talks to
  agents-platform.
- **`invoke_kanban_agent`, `run_ready_cards`** — both dispatch/resume
  agents-platform runs. Not ported.
- **No comment byline.** The monolith prefixed every comment with
  `**agent_slug** — [run_id](url)`, resolved by querying agents-platform's
  `/api/runs`. That lookup doesn't exist here, so the byline is dropped
  rather than faked. Put one in your comment text if you want it.
- **No stale-card auto-archival.** That swept `Done`/`Self-closed` cards
  older than 7 days into the `*_archived` statuses, piggybacked on Notion
  webhook traffic. There is no webhook here to piggyback on. The archive
  statuses still exist and can be set manually.

**Attaching files — `attach_kanban_file` works now.**

Pass an **absolute** path and it uploads the bytes to Notion and appends a
block: images render inline, PDFs get a viewer, anything else becomes a
download chip. Two things to know before you call it:

- The path is read from **the aw-workspace filesystem**, not from wherever
  you are reading files. If you generated the artefact inside another
  container, write it to `.tmp/` first — that is shared — and pass that path.
- Notion's single-part upload stops at 20 MB. Bigger than that, link to it
  with `add_kanban_comment` instead and say plainly that it's a link.

**Attaching a presentation — use `attach_kanban_presentation`, not the two
steps.** Pass a `presentation_id` and it exports the deck to PNG, attaches
that as an image block, and appends a live share link right after it. Do it
this way rather than `export_presentation_to_image` + `attach_kanban_file`:
the export alone is a snapshot that goes stale the next time the deck is
edited, and the pair keeps a current link beside the picture.

If the reply comes back with `shared: false`, the image landed but no link
was written — either this workspace has no published URL, or the share call
failed; the `note` says which. That is not a reason to retry the whole call,
which would attach the image a second time.

**Not ported here, on purpose:**

- **`invoke_kanban_agent` / `run_ready_cards`.** Both dispatch and resume
  agents-platform runs. This app doesn't talk to an orchestrator, so they
  belong in aw-app-agents-platform-runners — ask there, not here.

If a run genuinely needs the dispatch half (fire a run on `ready`, Telegram
approval), that still lives in the monolith at
`agentic-workspace/src/api/routes/notion_kanban.py`. It has not been
decoupled.

## Loading these tools

`ToolSearch` with `select:<name>` for each tool you'll need, up front,
instead of guessing keywords. Typical dev-run set:

```
select:mcp__aw-gateway__aw__aw_kanban__add_kanban_comment,mcp__aw-gateway__aw__aw_kanban__set_blocker
```

Typical QA-run set:

```
select:mcp__aw-gateway__aw__aw_kanban__set_qa_status,mcp__aw-gateway__aw__aw_kanban__set_blocker,mcp__aw-gateway__aw__aw_kanban__add_kanban_comment
```
