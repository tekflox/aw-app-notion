"""Builds this app's own root ``mcp.json`` — the file aw-mcp-gateway's
app-scan reads directly (same contract as aw-app-mcp-tools, see that repo's
``mcp_tools_app/plugin.py``: ``build_mcp_servers``/``write_mcp_json``).

Ported from agentic-workspace's own MCP config (``src/config/mcp.json``'s
``notion`` entry): ``npx -y @notionhq/notion-mcp-server``, upstream package
``@notionhq/notion-mcp-server`` (official Notion MCP server). The monolith
wired the token in statically via ``OPENAPI_MCP_HEADERS``; here it's read
from this app's own secret store instead (``ctx.secrets`` — see plugin.py)
and never written to disk anywhere but this generated file, which is
git-ignored the same way aw-app-mcp-tools's regenerated mcp.json is not
committed as a "real" credential (this app's own mcp.json ships with the
server DISABLED — no token, no entry — until a token is saved).
"""
from __future__ import annotations

import json
from pathlib import Path

SERVER_NAME = "notion"


def build_mcp_servers(token: str | None) -> dict:
    """The ``mcpServers`` object this app's root mcp.json should contain.
    Empty (no ``notion`` key at all) when no token has been saved yet — the
    gateway's app-scan just sees nothing to add, rather than a broken/
    disabled entry it would otherwise have to special-case."""
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
        }
    }


def write_mcp_json(package_dir: str, token: str | None) -> dict:
    """Regenerate this app's own root mcp.json from the stored token and
    write it to disk. Returns the full ``{"mcpServers": ...}`` document
    written."""
    doc = {"mcpServers": build_mcp_servers(token)}
    path = Path(package_dir) / "mcp.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc
