"""
notion_app's backend sub-app.

Unlike aw-app-template's mode-agnostic ``build_routes()`` (no ``ctx``, works
standalone too), this app has **no standalone mode** — every route here
needs ``ctx.secrets`` (the token store) and ``ctx.package_dir`` (where this
app's own ``mcp.json`` gets regenerated), both of which only exist inside
the real F4 framework runtime. Same reasoning as aw-app-git, which has no
``__main__.py`` either.

``build_routes(ctx)`` is called once from ``plugin.py``'s ``activate()`` and
mounted at ``/api/apps/notion`` behind the runtime's ``IdentityGuard`` — see
``docs/knowledge_base/docs/architecture/adr-app-front-back-routes-dual-mode.md``.

The token itself is intentionally **not** routed through the generic
``POST /api/apps/notion/config`` endpoint (which would land it in
``loaded.config`` — plain, cloud-syncable app config, see
``src/apps/routes.py``'s ``save_app_config``). ``POST /settings`` here goes
straight to ``ctx.secrets`` instead, same pattern as aw-app-git's
``github_token`` — the config_schema's ``notion_token`` field exists only so
the settings UI knows to render it as a password input (``x-secret``); its
value is read here, in this app's own route, not by the generic config path.
"""
from __future__ import annotations

from fastapi import Body, FastAPI

from . import mcp_config

TOKEN_KEY = "notion_token"


def build_routes(ctx) -> FastAPI:
    app = FastAPI(title="notion")

    @app.get("/status")
    async def status() -> dict:
        token = ctx.secrets.read(TOKEN_KEY)
        configured = bool(token)
        return {
            # "logged_in" is what the windows/main.json auth_status widget
            # binds to; "configured" is the same value under a name that
            # makes more sense outside that widget (tests, /mcp.json callers).
            "logged_in": configured,
            "configured": configured,
            "mcp_server_enabled": bool(mcp_config.build_mcp_servers(token)),
        }

    @app.post("/settings")
    async def save_settings(data: dict = Body(...)) -> dict:
        """Store the Notion token (secrets:own) and regenerate this app's
        own mcp.json from it. Does NOT hot-reload MCP Gateway — that
        requires aw-workspace core to call the gateway's internal /reload
        (see aw-app-mcp-tools/plugin.py's contributes.mcp.reload_on_save,
        which only fires off the generic config-save path this app
        deliberately avoids for the token). Reinstalling/restarting the
        mcp-gateway app picks up a freshly-saved token."""
        token = (data.get(TOKEN_KEY) or "").strip()
        if not token:
            return {"error": f"{TOKEN_KEY} is required"}
        ctx.secrets.write(TOKEN_KEY, token)
        doc = mcp_config.write_mcp_json(ctx.package_dir, token)
        return {
            "ok": True,
            "logged_in": True,
            "configured": True,
            "mcp_server_enabled": bool(doc["mcpServers"]),
        }

    @app.post("/logout")
    async def clear_token() -> dict:
        ctx.secrets.delete(TOKEN_KEY)
        mcp_config.write_mcp_json(ctx.package_dir, None)
        return {"ok": True, "logged_in": False, "configured": False}

    @app.get("/mcp.json")
    async def mcp_json() -> dict:
        """Same document this app wrote to disk — convenience for
        inspecting what the gateway will see, without a shell into the
        container."""
        token = ctx.secrets.read(TOKEN_KEY)
        return {"mcpServers": mcp_config.build_mcp_servers(token)}

    return app
