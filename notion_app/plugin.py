"""
Entrypoint referenced by aw-app.json's runtime.entrypoint
("notion_app.plugin:NotionAppPlugin").

Plugs into the real F4 framework runtime: activate(ctx) (1) regenerates this
app's own root mcp.json from whatever token is already in the secret store
(picks up a token saved before a workspace recreation/reconcile — same
reasoning as aw-app-mcp-tools re-writing its mcp.json on activate), and
(2) registers the settings + Kanban sub-app from routes.py THROUGH the gated
``ctx.routes`` facade (capability ``routes:register``), mounted by the
runtime at ``/api/apps/notion``.

The generated mcp.json advertises two servers — the upstream
``@notionhq/notion-mcp-server`` and this app's own in-process ``aw-kanban``
tools. The latter's entry has to be rebuilt on every activate rather than
persisted: it embeds this process's hostname and API key, both of which
change when the workspace container is recreated.

No system CLI to install (the notion MCP server itself runs as an
``npx``-spawned subprocess of MCP Gateway, not of this app — mirrors how
aw-app-mcp-tools's Playwright entry needs no local install either).
"""

from __future__ import annotations

import logging
import os

from . import mcp_config
from . import routes as routes_mod
from .kanban.config import KanbanConfig

log = logging.getLogger("aw_apps.notion")


class NotionAppPlugin:
    async def activate(self, ctx) -> None:
        self._ctx = ctx
        token = ctx.secrets.read(routes_mod.TOKEN_KEY)
        doc = self._write_mcp_json(ctx, token)

        ctx.routes.register(routes_mod.build_routes(ctx))

        kanban = KanbanConfig(ctx)
        log.info(
            "aw-app-notion activated: mcp servers=%s, kanban board=%s, routes mounted",
            sorted(doc["mcpServers"]) or "none",
            kanban.database_id or "not configured",
        )

    async def on_config_changed(self, ctx) -> None:
        """Core calls this after ``ctx.config`` is updated. Nothing to rebuild
        for the Kanban settings themselves — ``KanbanConfig`` re-reads
        ``ctx.config`` on every access — this is here only to log what the
        board is now pointed at, since a silently-wrong database_id is the
        one failure that looks identical to a permissions problem."""
        log.info("aw-app-notion config changed: kanban board=%s",
                 KanbanConfig(ctx).database_id or "not configured")

    def _write_mcp_json(self, ctx, token: str | None) -> dict:
        port = int(os.environ.get("AW_PORT") or 9030)
        return mcp_config.write_mcp_json(ctx.package_dir, token, port=port)

    async def deactivate(self) -> None:
        log.info("aw-app-notion deactivated")
