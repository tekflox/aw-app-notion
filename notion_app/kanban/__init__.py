"""Agents Kanban board support, ported out of agentic-workspace.

The monolith split this across three coupled pieces:

* ``src/api/routes/notion_kanban.py`` — REST surface + the Notion webhook
  that fires agents-platform runs when a card hits "Ready".
* ``src/api/kanban_manager.py``       — Notion property/comment plumbing.
* ``src/mcp/kanban.py``               — a stdio MCP server that did nothing
  but forward to that REST surface over ``http://127.0.0.1:9123`` with an
  ``.tmp/awserv_api_key`` it read off disk.

Only the board itself is portable. The webhook dispatch, the Telegram
approval keyboard, ``invoke_kanban_agent`` and ``run_ready_cards`` are
agents-platform integration, not Notion — they stay in the monolith and are
deliberately **not** reimplemented here (see README).

What lives here is the part that is purely "a Notion database used as a
Kanban board": read it, create cards, move them, comment, set/read
properties. It talks to ``api.notion.com`` directly with this app's own
stored token, so there is no awserv hop and no second API key to provision.
"""
