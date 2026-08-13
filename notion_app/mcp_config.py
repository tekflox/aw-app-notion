"""Builds this app's own root ``mcp.json`` — the file aw-mcp-gateway's
app-scan reads directly (same contract as aw-app-mcp-tools, see that repo's
``mcp_tools_app/plugin.py``: ``build_mcp_servers``/``write_mcp_json``).

Two servers are advertised from this one file:

``notion``
    The official upstream ``@notionhq/notion-mcp-server``, spawned by the
    gateway as an ``npx`` subprocess. Ported from agentic-workspace's
    ``src/config/mcp.json``. The monolith wired the token in statically via
    ``OPENAPI_MCP_HEADERS``; here it comes from this app's own secret store
    (``ctx.secrets`` — see plugin.py) and never lands anywhere but this
    generated file. No token → no entry, rather than a broken one the gateway
    would have to special-case.

``aw-kanban``
    This app's *own* Kanban tools, served in-process over Streamable HTTP at
    ``/api/apps/notion/mcp`` (see ``mcp/http_handler.py``). Ported from the
    monolith's ``src/mcp/kanban.py``. Advertised whenever a token exists —
    the board's ``kanban_database_id`` may still be unset, in which case the
    tools load and return a clear "not configured" error, which beats the
    tools silently not existing.
"""
from __future__ import annotations

import json
from pathlib import Path

from .mcp import self_register

SERVER_NAME = "notion"
KANBAN_SERVER_NAME = self_register.MCP_SERVER_NAME


def build_mcp_servers(token: str | None, *, port: int | None = None) -> dict:
    """The ``mcpServers`` object this app's root mcp.json should contain.

    Empty when no token has been saved yet — the gateway's app-scan just sees
    nothing to add. Neither server can do anything without a token, so they
    appear and disappear together.
    """
    if not token:
        return {}
    return {
        SERVER_NAME: {
            "enabled": True,
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "env": {
                "NOTION_TOKEN": token,
            },
        },
        KANBAN_SERVER_NAME: self_register.build_self_entry(port),
    }


def write_mcp_json(package_dir: str, token: str | None, *, port: int | None = None) -> dict:
    """Regenerate this app's own root mcp.json from the stored token and
    write it to disk. Returns the full ``{"mcpServers": ...}`` document
    written."""
    doc = {"mcpServers": build_mcp_servers(token, port=port)}
    path = Path(package_dir) / "mcp.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc
