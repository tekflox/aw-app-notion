"""The ``aw-kanban`` MCP server, over Streamable HTTP (``POST /mcp``).

Ported from agentic-workspace's ``src/mcp/kanban.py``. That file was a stdio
server whose every handler did nothing but forward to an awserv REST route;
here the handlers call :class:`~notion_app.kanban.cards.KanbanBoard` directly,
in-process, so there is no second hop and no second credential.

Three of the monolith's eleven tools are deliberately absent —
``attach_kanban_presentation``, ``invoke_kanban_agent`` and
``run_ready_cards``. They are agents-platform / aw-app-presentations
integration rather than Notion (see ``notion_app/kanban/cards.py``'s module
docstring).

``attach_kanban_file`` was the fourth until it was ported: the monolith
reached Notion's file-upload API through an awserv route that proxied the
bytes, so it needed a stdlib multipart POST against ``/v1/file_uploads``
before it could ship. That now lives in ``NotionClient.upload_file``.

Two are new: ``list_kanban_cards`` and ``get_kanban_card``. The monolith had
no way for an agent to *read* the board at all — ``_list_kanban_cards``
existed in its source but was never added to its ``TOOLS`` list, so it was
unreachable.
"""
from __future__ import annotations

import json
import logging

from fastapi.concurrency import run_in_threadpool

from ..kanban.cards import KanbanBoard
from ..kanban.client import NotionError

log = logging.getLogger("aw_apps.notion.kanban")

SERVER_NAME = "aw-kanban"
SERVER_VERSION = "1.0.0"

_PAGE_ID_DESC = (
    "Notion page id of the Kanban card. Optional — auto-filled from this run's "
    "Kanban-card context (NOTION_TASK_ID) when omitted; pass it explicitly only "
    "to target a different card."
)


def _ok(req_id, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": False}}


def _err(req_id, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": True}}


def _page_id(args: dict) -> str:
    """page_id from the call's own arguments, falling back to the gateway-
    injected per-connection context (``X-Aw-Context-Notion-Task-Id`` →
    ``_aw_context.NOTION_TASK_ID``). Ported verbatim from the monolith: it
    lets a card-bound run omit page_id entirely instead of depending on the
    model reading an env var and passing it correctly on every call."""
    explicit = (args.get("page_id") or "").strip()
    if explicit:
        return explicit
    ctx = args.get("_aw_context") or {}
    return (ctx.get("NOTION_TASK_ID") or "").strip()


TOOLS_SCHEMA: list[dict] = [
    {
        "name": "list_kanban_cards",
        "description": (
            "List cards on the Agents Kanban board, newest first. Omit `status` to see "
            "every card regardless of status, or pass a logical key ('backlog', 'ready', "
            "'running', 'need_human', 'done', 'ready_to_deploy', 'auto_resolved', "
            "'planned', 'archived') or a raw Notion option name. Returns page_id, title, "
            "status, priority, source, agent/target slug, tags, occurrence count, "
            "timestamps and the card URL — start here when you need a page_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Logical status key or Notion option name. Omit for all statuses."},
                "source": {"type": "string", "description": "Filter by the Source property, e.g. 'system-analyst'."},
                "limit": {"type": "integer", "default": 25, "description": "Max cards to return (1-100)."},
                "order": {"type": "string", "enum": ["created", "edited"], "default": "created",
                          "description": "Sort by creation time (default) or last edit."},
            },
        },
    },
    {
        "name": "get_kanban_card",
        "description": (
            "Read one card's summary — title, status, priority, slugs, tags, timestamps, "
            "URL. Use get_kanban_properties instead when you need arbitrary/custom "
            "properties rather than this fixed summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"page_id": {"type": "string", "description": _PAGE_ID_DESC}},
        },
    },
    {
        "name": "create_kanban_task",
        "description": (
            "Create a Notion Kanban finding card. Deduplicates by finding_key: an existing "
            "card with the same key is bumped instead (OccurrenceCount +1, LastSeenAt "
            "updated, a '🔁 Re-detectado' callout appended) and a card that was already "
            "Done is reopened to Backlog as a regression. The card is created in Backlog. "
            "NOTE: unlike the monolith's version of this tool, no Telegram approval is "
            "sent and no agent run is dispatched — that is agents-platform's job and is "
            "not wired into this app. Returns {page_id, url, is_new, occurrence_count}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Card title — short, descriptive."},
                "finding_key": {"type": "string", "description": "Stable dedup slug: 'category:subject-kebab', e.g. 'resilience:no-health-check-caddy'."},
                "priority": {"type": "string", "enum": ["Alta", "Média", "Baixa"], "description": "Finding priority."},
                "agent_slug": {"type": "string", "description": "Executor agent slug (e.g. 'coder-sonnet', 'doc-writer', 'debugger')."},
                "target_slug": {"type": "string", "description": "agents-platform target slug. Recorded on the card only — not validated, since this app doesn't talk to agents-platform."},
                "input_text": {"type": "string", "description": "Task instructions for the executor, written into the card body. Be specific about what to fix and where."},
                "check_hint": {"type": "string", "description": "Bash one-liner that exits 0 if the issue is gone."},
                "description": {"type": "string", "description": "Short summary (≤200 chars) rendered as a callout at the top of the card."},
                "plan": {"type": "string", "description": "Full analysis and execution plan for the card body: what was found, how detected, impact, step-by-step fix, verification."},
                "source": {"type": "string", "description": "Agent that created this finding, e.g. 'system-analyst'."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags, e.g. ['resilience', 'docker']. Max 5."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "move_kanban_task",
        "description": (
            "Move a Kanban card to a new Status. If `comment` is given it is posted on the "
            "card BEFORE the status changes. Moving to 'need_human' REQUIRES a comment "
            "explaining the problem, the options, and what needs deciding — the call is "
            "rejected without one. NOTE: moving to 'ready' only sets the status here; it "
            "does not dispatch an agent run (agents-platform owns dispatch)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": _PAGE_ID_DESC},
                "status": {"type": "string", "description": "Logical status key ('backlog', 'ready', 'running', 'ready_to_deploy', 'need_human', 'done', 'auto_resolved', 'planned', 'archived') or a raw Notion option name."},
                "comment": {"type": "string", "description": "Comment to post before moving. Required when status='need_human'."},
            },
            "required": ["status"],
        },
    },
    {
        "name": "add_kanban_comment",
        "description": (
            "Post a plain comment on a Kanban card without changing its status — progress "
            "notes, questions, delivery reports. Markdown [label](url) links become real "
            "Notion hyperlinks. Long text is chunked to Notion's 2000-char rich_text limit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": _PAGE_ID_DESC},
                "text": {"type": "string", "description": "Comment text to post."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "set_kanban_property",
        "description": (
            "Set any one property on a Kanban card by name — 'is_live', "
            "'is_deployment_needed', 'Priority', or anything else in the board's schema. "
            "The property's Notion type (checkbox, select, status, rich_text, number, date, "
            "multi_select, url, title) is looked up from the live schema, so pass a plain "
            "value: true/false, a string, a number, or a list for multi_select. Pass "
            "value=null to clear it. An unknown property name fails with the list of "
            "properties the board actually has."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": _PAGE_ID_DESC},
                "property": {"type": "string", "description": "Property name, e.g. 'is_live', 'Priority'."},
                "value": {"description": "Value to set — true/false, string, number, list of strings, or null to clear."},
            },
            "required": ["property", "value"],
        },
    },
    {
        "name": "get_kanban_properties",
        "description": (
            "Read one or more properties off a Kanban card as plain values — no need to "
            "know each property's Notion type shape. Omit `properties` to get all of them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": _PAGE_ID_DESC},
                "properties": {"type": "array", "items": {"type": "string"},
                               "description": "Optional list of property names to limit the response to."},
            },
        },
    },
    {
        "name": "set_qa_status",
        "description": (
            "MANDATORY for QA agents: call exactly once at the end of every QA review to "
            "record your verdict. Stamps the card's QAStatus property (when the board has "
            "one) and moves the card. With no page_id and no card context it is a no-op on "
            "Notion — the verdict is still recorded in this run's own log, so call it every "
            "time regardless."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": _PAGE_ID_DESC + " Leave unset if this run has no card."},
                "status": {"type": "string", "enum": ["done", "ready_to_deploy", "need_human"],
                           "description": "Your verdict: passed (done/ready_to_deploy) or needs a human decision (need_human)."},
                "comment": {"type": "string", "description": "Comment to post before moving. Required when status='need_human'."},
            },
            "required": ["status"],
        },
    },
    {
        "name": "set_blocker",
        "description": (
            "Call this the moment you're blocked mid-task — a tool you need doesn't exist, "
            "you lack access, the requirement is ambiguous and you can't guess safely. "
            "Posts a '🚧 Blocker' comment and moves the card straight to Need Human. Don't "
            "burn retries hunting for a workaround first — call this promptly, explaining "
            "what you tried and what would unblock you. Equivalent to "
            "move_kanban_task(status='need_human'); use whichever you reach for first. "
            "NOTE: no Telegram ping is sent from here — that notification is "
            "agents-platform's."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": _PAGE_ID_DESC},
                "comment": {"type": "string", "description": "What's blocking you, what you already tried, and exactly what's needed to unblock. Required."},
            },
            "required": ["comment"],
        },
    },
    {
        "name": "attach_kanban_file",
        "description": (
            "Attach a local file to a Kanban card. Images (png/jpg/gif/webp/svg) render "
            "inline, PDFs get a viewer, anything else becomes a download chip. Use this for "
            "evidence a comment can't carry — a screenshot of the bug, a failing log, a "
            "generated report.\n\n"
            "The path is read from the aw-workspace filesystem, NOT from wherever you are "
            "reading files: write the file to `.tmp/` (shared) first if you generated it "
            "elsewhere. Max 20 MB."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": _PAGE_ID_DESC},
                "file_path": {"type": "string", "description": "Absolute path to the file, e.g. '/opt/aw-workspace/.tmp/review/failing-test.png'."},
            },
            "required": ["file_path"],
        },
    },
]


# ── handlers ────────────────────────────────────────────────────────────
# Each returns a plain JSON-serialisable result, or raises. They run in a
# threadpool (urllib is blocking) — never call these on the event loop.

def _h_list(board: KanbanBoard, args: dict) -> dict:
    return board.list_cards(
        status=args.get("status") or "", source=args.get("source") or "",
        limit=args.get("limit") or 25, order=args.get("order") or "created")


def _h_get_card(board: KanbanBoard, args: dict) -> dict:
    page_id = _page_id(args)
    if not page_id:
        raise ValueError("page_id is required")
    return board.get_card(page_id)


def _h_create(board: KanbanBoard, args: dict) -> dict:
    return board.create_card(
        title=args.get("title") or "", finding_key=args.get("finding_key") or "",
        priority=args.get("priority") or "Média", agent_slug=args.get("agent_slug") or "",
        target_slug=args.get("target_slug") or "", input_text=args.get("input_text") or "",
        check_hint=args.get("check_hint") or "", description=args.get("description") or "",
        plan=args.get("plan") or "", source=args.get("source") or "",
        tags=args.get("tags") or [])


def _h_move(board: KanbanBoard, args: dict) -> dict:
    page_id = _page_id(args)
    if not page_id:
        raise ValueError("page_id is required")
    return board.move_card(page_id, args.get("status") or "", args.get("comment") or "")


def _h_comment(board: KanbanBoard, args: dict) -> dict:
    page_id = _page_id(args)
    if not page_id:
        raise ValueError("page_id is required")
    return board.add_comment(page_id, args.get("text") or "")


def _h_set_prop(board: KanbanBoard, args: dict) -> dict:
    page_id = _page_id(args)
    prop = (args.get("property") or "").strip()
    if not page_id or not prop:
        raise ValueError("page_id and property are required")
    if "value" not in args:
        raise ValueError("value is required (pass null to clear the property)")
    return board.set_property(page_id, prop, args.get("value"))


def _h_get_props(board: KanbanBoard, args: dict) -> dict:
    page_id = _page_id(args)
    if not page_id:
        raise ValueError("page_id is required")
    return board.get_properties(page_id, args.get("properties") or None)


def _h_qa(board: KanbanBoard, args: dict) -> dict:
    return board.set_qa_status(_page_id(args), args.get("status") or "",
                               args.get("comment") or "")


def _h_blocker(board: KanbanBoard, args: dict) -> dict:
    page_id = _page_id(args)
    if not page_id:
        raise ValueError("page_id is required")
    return board.set_blocker(page_id, args.get("comment") or "")


def _h_attach_file(board: KanbanBoard, args: dict) -> dict:
    page_id = _page_id(args)
    if not page_id:
        raise ValueError("page_id is required")
    return board.attach_file(page_id, args.get("file_path") or "")


HANDLERS = {
    "list_kanban_cards": _h_list,
    "get_kanban_card": _h_get_card,
    "create_kanban_task": _h_create,
    "move_kanban_task": _h_move,
    "add_kanban_comment": _h_comment,
    "set_kanban_property": _h_set_prop,
    "get_kanban_properties": _h_get_props,
    "set_qa_status": _h_qa,
    "set_blocker": _h_blocker,
    "attach_kanban_file": _h_attach_file,
}


async def handle_request(request: dict, *, board: KanbanBoard) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}}
    if method != "tools/call":
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}}

    name = request.get("params", {}).get("name", "")
    args = request.get("params", {}).get("arguments", {}) or {}
    handler = HANDLERS.get(name)
    if not handler:
        return _err(req_id, f"Unknown tool: {name}")

    try:
        result = await run_in_threadpool(handler, board, args)
    except ValueError as exc:
        return _err(req_id, str(exc))
    except NotionError as exc:
        # Surface Notion's own body: "object_not_found" here almost always
        # means the integration was never shared with the page/database, which
        # no amount of retrying from this side will fix.
        return _err(req_id, f"{name} failed — {exc}")
    except Exception as exc:  # noqa: BLE001 - last resort, must not 500 the route
        log.exception("kanban MCP tool %s failed", name)
        return _err(req_id, f"{name} failed: {exc}")

    if isinstance(result, dict) and result.get("ok") is False:
        return _err(req_id, json.dumps(result, ensure_ascii=False, indent=2))
    return _ok(req_id, json.dumps(result, ensure_ascii=False, indent=2))
