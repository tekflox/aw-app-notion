"""
Entrypoint referenced by aw-app.json's runtime.entrypoint
("notion_app.plugin:NotionAppPlugin").

Plugs into the real F4 framework runtime: activate(ctx) (1) regenerates this
app's own root mcp.json from whatever token is already in the secret store
(picks up a token saved before a workspace recreation/reconcile — same
reasoning as aw-app-mcp-tools re-writing its mcp.json on activate), and
(2) registers the settings sub-app from routes.py THROUGH the gated
``ctx.routes`` facade (capability ``routes:register``), mounted by the
runtime at ``/api/apps/notion``.

No system CLI to install (the notion MCP server itself runs as an
``npx``-spawned subprocess of MCP Gateway, not of this app — mirrors how
aw-app-mcp-tools's Playwright entry needs no local install either).
"""

from __future__ import annotations

import logging

from . import mcp_config
from . import routes as routes_mod

log = logging.getLogger("aw_apps.notion")


class NotionAppPlugin:
    async def activate(self, ctx) -> None:
        token = ctx.secrets.read(routes_mod.TOKEN_KEY)
        doc = mcp_config.write_mcp_json(ctx.package_dir, token)

        ctx.routes.register(routes_mod.build_routes(ctx))

        log.info(
            "aw-app-notion activated: mcp.json server enabled=%s, routes mounted",
            bool(doc["mcpServers"]),
        )

    async def deactivate(self) -> None:
        log.info("aw-app-notion deactivated")
