---
repo: architecture
path: docs/architecture/aw-app-notion.md
source: generated
edited: false
checksum: sha256:b6de7281a117ea24054de3462237e759d70ba259a31e301832219464ab5ae6d0
---
# Notion

- **repo**: aw-app-notion
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Ports agentic-workspace's Notion integration into aw-workspace: stores a Notion internal-integration token in the zero-knowledge secret store and generates the mcp.json entries MCP Gateway scans — both the generic @notionhq/notion-mcp-server and this app's own aw-kanban server, which turns a Notion database into the Agents Kanban board (list/create/move/comment on cards, set QA status, flag blockers).

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/notion
- `stdio-mcp` → **mcp-gateway** — MCP surface aggregated by the gateway

## MCP tools
- `add_kanban_comment`
- `comments`
- `create-a-data-source`
- `create_kanban_task`
- `get_kanban_card`
- `get_kanban_properties`
- `list-data-source-templates`
- `list_kanban_cards`
- `move_kanban_task`
- `move-page`
- `query-data-source`
- `retrieve-a-database`
- `retrieve-a-data-source`
- `retrieve-page-markdown`
- `search`
- `set_blocker`
- `set_kanban_property`
- `set_qa_status`
- `update-a-data-source`
- `update-page-markdown`

## Requirements
_none documented_
