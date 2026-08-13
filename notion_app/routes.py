"""
notion_app's backend sub-app.

Unlike aw-app-template's mode-agnostic ``build_routes()`` (no ``ctx``, works
standalone too), this app has **no standalone mode** — every route here
needs ``ctx.secrets`` (the token store), ``ctx.config`` (the Kanban board
settings) and ``ctx.package_dir`` (where this app's own ``mcp.json`` gets
regenerated), all of which only exist inside the real F4 framework runtime.
Same reasoning as aw-app-git, which has no ``__main__.py`` either.

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

The Kanban board settings (``kanban_database_id``, ``kanban_statuses``) are
the opposite case: a database id is not a credential, so it rides the generic
config path and is read back off ``ctx.config``.
"""
from __future__ import annotations

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, Response

from . import mcp_config, sync as sync_mod
from .job import SyncJob
from .kanban.cards import KanbanBoard
from .kanban.client import NotionClient, NotionError
from .kanban.config import KanbanConfig

TOKEN_KEY = "notion_token"


def build_routes(ctx) -> FastAPI:
    app = FastAPI(title="notion")

    # Resolved per call, never snapshotted: a token saved (or cleared) at
    # runtime has to take effect without a restart.
    client = NotionClient(lambda: ctx.secrets.read(TOKEN_KEY))
    kanban_cfg = KanbanConfig(ctx)
    board = KanbanBoard(client, kanban_cfg)
    # One slot per app instance — see job.py for why it isn't a queue.
    job = SyncJob()

    def _kanban(fn, *args, **kwargs):
        """Run a board op, turning a NotionError into its real HTTP status
        instead of a 500 — a 404 from Notion ("shared with the integration?")
        and a 503 from us ("no database_id") are different problems and the
        caller can only tell them apart if we keep the codes."""
        try:
            return fn(*args, **kwargs)
        except NotionError as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=exc.status if 400 <= exc.status < 600 else 502)

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
            "kanban": {
                "configured": kanban_cfg.configured,
                "database_id": kanban_cfg.database_id,
                "statuses": kanban_cfg.statuses,
            },
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
            "mcp_servers": sorted(doc["mcpServers"].keys()),
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

    # ------------------------------------------------------------------
    # Kanban — a REST mirror of the MCP tools. The MCP surface is what
    # agents use; these exist so the board is reachable from curl, the UI
    # and tests without speaking JSON-RPC.
    # ------------------------------------------------------------------

    @app.get("/kanban/cards")
    async def kanban_cards(status: str = "", source: str = "", limit: int = 25,
                           order: str = "created"):
        return _kanban(board.list_cards, status=status, source=source,
                       limit=limit, order=order)

    @app.get("/kanban/cards/{page_id}")
    async def kanban_card(page_id: str):
        return _kanban(board.get_card, page_id)

    @app.post("/kanban/cards")
    async def kanban_create(data: dict = Body(...)):
        return _kanban(board.create_card, **{
            k: data.get(k) for k in (
                "title", "finding_key", "priority", "agent_slug", "target_slug",
                "input_text", "check_hint", "description", "plan", "source", "tags")
            if data.get(k) is not None
        })

    @app.post("/kanban/move")
    async def kanban_move(data: dict = Body(...)):
        return _kanban(board.move_card, data.get("page_id") or "",
                       data.get("status") or "", data.get("comment") or "")

    @app.post("/kanban/comment")
    async def kanban_comment(data: dict = Body(...)):
        return _kanban(board.add_comment, data.get("page_id") or "",
                       data.get("text") or "")

    @app.get("/kanban/properties")
    async def kanban_get_properties(page_id: str, properties: str = ""):
        names = [p.strip() for p in properties.split(",") if p.strip()] or None
        return _kanban(board.get_properties, page_id, names)

    @app.post("/kanban/set-property")
    async def kanban_set_property(data: dict = Body(...)):
        return _kanban(board.set_property, data.get("page_id") or "",
                       data.get("property") or "", data.get("value"))

    # ------------------------------------------------------------------
    # Notion → knowledge base sync (the monolith's ``./aw notion-sync``).
    # The CLI command in commands/notion_sync.py is a thin client over
    # these: the token lives in this app's secret store, which no other
    # process can read, so the work has to happen here.
    # ------------------------------------------------------------------

    @app.get("/sync/state")
    async def sync_state() -> dict:
        cfg = getattr(ctx, "config", None) or {}
        has_token = bool(ctx.secrets.read(TOKEN_KEY))
        return {
            **sync_mod.sync_state(),
            "root_page_id": cfg.get(sync_mod.ROOT_PAGE_KEY) or "",
            "bidirectional": bool(cfg.get(sync_mod.BIDIRECTIONAL_KEY, False)),
            "kanban_comments": bool(cfg.get(sync_mod.COMMENTS_KEY, True)),
            # Each half reports separately: a workspace can legitimately
            # mirror only the board, or only the notes.
            "notes_configured": has_token and bool(cfg.get(sync_mod.ROOT_PAGE_KEY)),
            "kanban_configured": has_token and kanban_cfg.configured,
        }

    @app.get("/sync/job")
    async def sync_job() -> dict:
        """The current (or last) sync job. Poll this after POST /sync."""
        return job.snapshot()

    @app.post("/sync")
    async def start_sync(data: dict = Body(default={})):
        """Kick off a sync in the BACKGROUND and return 202 immediately.

        A full sync walks every child page, every card, every nested block,
        then waits on the KB reindex — minutes, not seconds. Holding the HTTP
        connection open for that does not survive the BYOD tunnel, whose edge
        drops a long-held request and answers ``502 workspace offline`` while
        the work is still running perfectly well on this side. That is the
        same wall core's ``POST /api/apps/install`` hit (see its docstring),
        and this is the same answer: 202 + a pollable job.

        Poll ``GET /sync/job`` until ``status`` leaves ``running``.
        """
        cfg = getattr(ctx, "config", None) or {}
        override = data.get("bidirectional")
        started = job.start(
            lambda: sync_mod.run_sync(
                client, cfg.get(sync_mod.ROOT_PAGE_KEY) or "", board,
                force=bool(data.get("force")),
                bidirectional=(bool(cfg.get(sync_mod.BIDIRECTIONAL_KEY, False))
                               if override is None else bool(override)),
                rebuild=data.get("rebuild", True) is not False,
                notes=data.get("notes", True) is not False,
                kanban=data.get("kanban", True) is not False,
                with_comments=bool(cfg.get(sync_mod.COMMENTS_KEY, True)),
            ))
        if not started:
            return JSONResponse({"ok": False, "error": "a sync is already running",
                                 "job": job.snapshot()}, status_code=409)
        return JSONResponse(job.snapshot(), status_code=202)

    # ------------------------------------------------------------------
    # MCP — Streamable HTTP, auto-discovered by aw-mcp-gateway's app-scan
    # (see mcp/self_register.py + mcp/http_handler.py).
    # ------------------------------------------------------------------

    @app.post("/mcp")
    async def mcp_post(data: dict | list = Body(...)):
        from .mcp.http_handler import handle_request as mcp_handle_request

        messages = data if isinstance(data, list) else [data]
        responses = []
        for m in messages:
            r = await mcp_handle_request(m, board=board)
            if r is not None:
                responses.append(r)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses if isinstance(data, list) else responses[0])

    @app.get("/mcp")
    async def mcp_get():
        return Response(status_code=405)

    return app
