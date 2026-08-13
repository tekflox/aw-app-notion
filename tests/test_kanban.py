"""Kanban board coverage — the ported half of agentic-workspace's
notion_kanban.py / kanban_manager.py / mcp/kanban.py.

Every Notion call goes through NotionClient.request, so a fake client that
records requests and replays canned responses covers the whole surface
without touching the network.

Run: .venv/aw/bin/python -m pytest tests/test_kanban.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notion_app.kanban.cards import KanbanBoard  # noqa: E402
from notion_app.kanban.client import (  # noqa: E402
    NotionError,
    build_property_payload,
    extract_property_value,
    page_title,
    split_text_blocks,
    status_property_name,
    text_to_rich_text,
)
from notion_app.kanban.config import DEFAULT_STATUSES, KanbanConfig  # noqa: E402
from notion_app.mcp import http_handler  # noqa: E402

DB_ID = "3645bf3b-9510-80d9-af78-d085fc94d571"

SCHEMA = {
    "Name": {"type": "title"},
    "Status": {"type": "select"},
    "Priority": {"type": "select"},
    "Source": {"type": "rich_text"},
    "FindingKey": {"type": "rich_text"},
    "TargetSlug": {"type": "rich_text"},
    "AgentSlug": {"type": "select"},
    "CheckHint": {"type": "rich_text"},
    "OccurrenceCount": {"type": "number"},
    "LastSeenAt": {"type": "date"},
    "Tags": {"type": "multi_select"},
    "is_live": {"type": "checkbox"},
}


def _page(page_id="page-1", title="A card", status="Backlog", occ=1):
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "created_time": "2026-08-12T16:02:00.000Z",
        "last_edited_time": "2026-08-12T16:02:00.000Z",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": title}]},
            "Status": {"type": "select", "select": {"name": status}},
            "Priority": {"type": "select", "select": {"name": "High"}},
            "OccurrenceCount": {"type": "number", "number": occ},
            "Tags": {"type": "multi_select", "multi_select": [{"name": "resilience"}]},
        },
    }


class FakeClient:
    """Stands in for NotionClient — same three-arg request() contract."""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}
        self.configured = True

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        for (m, p), resp in self.responses.items():
            if m == method and p == path:
                return resp(body) if callable(resp) else resp
        return {}

    # the same convenience wrappers KanbanBoard uses
    def get_page(self, page_id):
        return self.request("GET", f"/pages/{page_id}")

    def patch_page(self, page_id, properties):
        return self.request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def create_page(self, body):
        return self.request("POST", "/pages", body)

    def query_database(self, database_id, body):
        return self.request("POST", f"/databases/{database_id}/query", body)

    def database_schema(self, database_id):
        return self.request("GET", f"/databases/{database_id}").get("properties", {})

    def append_blocks(self, page_id, children):
        return self.request("PATCH", f"/blocks/{page_id}/children", {"children": children})

    def post_comment(self, page_id, rich_text):
        return self.request("POST", "/comments",
                            {"parent": {"page_id": page_id}, "rich_text": rich_text})


class FakeCtx:
    def __init__(self, config=None):
        self.config = config if config is not None else {"kanban_database_id": DB_ID}


def _board(responses=None, config=None):
    client = FakeClient(responses)
    return KanbanBoard(client, KanbanConfig(FakeCtx(config))), client


# ── config ──────────────────────────────────────────────────────────────

def test_statuses_default_to_the_monolith_map():
    cfg = KanbanConfig(FakeCtx())
    assert cfg.statuses == DEFAULT_STATUSES
    assert cfg.notion_status("auto_resolved") == "Self-closed"
    assert cfg.notion_status("need_human") == "Need Human"


def test_status_override_merges_rather_than_replaces():
    """Renaming one Notion option must not require restating the other ten."""
    cfg = KanbanConfig(FakeCtx({"kanban_statuses": {"done": "Concluído"}}))
    assert cfg.notion_status("done") == "Concluído"
    assert cfg.notion_status("backlog") == "Backlog"


def test_unknown_status_key_passes_through():
    """Agents in the wild pass raw Notion option names; the monolith accepted
    them, so rejecting them here would be a regression."""
    assert KanbanConfig(FakeCtx()).notion_status("Ready to Deploy") == "Ready to Deploy"


def test_config_is_read_lazily_not_snapshotted():
    ctx = FakeCtx({})
    cfg = KanbanConfig(ctx)
    assert cfg.configured is False
    ctx.config = {"kanban_database_id": DB_ID}  # core rebinds this on save
    assert cfg.database_id == DB_ID


# ── client helpers ──────────────────────────────────────────────────────

def test_split_text_blocks_prefers_paragraph_breaks():
    text = ("a" * 1500) + "\n\n" + ("b" * 1000)
    chunks = split_text_blocks(text, max_len=2000)
    assert chunks[0] == "a" * 1500
    assert chunks[1] == "b" * 1000


def test_markdown_links_become_real_notion_links():
    rt = text_to_rich_text("see [the run](https://example.com/x) for details")
    assert rt[1]["text"]["link"]["url"] == "https://example.com/x"
    assert rt[1]["text"]["content"] == "the run"


def test_property_payload_roundtrip():
    for ptype, value in [("checkbox", True), ("select", "Done"), ("number", 3.0),
                         ("url", "https://x.dev"), ("multi_select", ["a", "b"])]:
        payload = build_property_payload(ptype, value)
        assert extract_property_value({"type": ptype, **payload}) == value


def test_text_properties_write_content_but_read_plain_text():
    """Notion's write shape and read shape differ for rich_text/title — you
    send `text.content`, you get back `plain_text`. Asserted explicitly so a
    future "simplification" of either helper to match the other gets caught."""
    assert build_property_payload("rich_text", "hello") == {
        "rich_text": [{"text": {"content": "hello"}}]}
    assert extract_property_value(
        {"type": "rich_text", "rich_text": [{"plain_text": "hello"}]}) == "hello"


def test_native_status_type_is_supported():
    """The monolith's board used a Select for Status and its helper raised on
    Notion's newer native `status` type — a board created today would have
    failed every move."""
    assert build_property_payload("status", "Done") == {"status": {"name": "Done"}}
    assert extract_property_value({"type": "status", "status": {"name": "Done"}}) == "Done"


def test_clearing_a_property_uses_the_per_type_empty_shape():
    assert build_property_payload("multi_select", None) == {"multi_select": []}
    assert build_property_payload("select", None) == {"select": None}


def test_title_is_found_by_type_not_by_name():
    """The monolith wrote `Name` but read `Task` in places; a board renamed in
    Notion's UI broke both."""
    assert page_title(_page(title="Renamed board")) == "Renamed board"
    assert page_title({"properties": {"Tarefa": {"type": "title",
                                                 "title": [{"plain_text": "pt-BR"}]}}}) == "pt-BR"


def test_status_property_name_falls_back_to_first_select():
    assert status_property_name(SCHEMA) == "Status"
    assert status_property_name({"Estado": {"type": "status"}}) == "Estado"


# ── board reads ─────────────────────────────────────────────────────────

def test_list_cards_sorts_newest_first_and_summarises():
    board, client = _board({
        ("POST", f"/databases/{DB_ID}/query"): {"results": [_page()], "has_more": False},
    })
    result = board.list_cards(limit=3)
    _, _, body = client.calls[0]
    assert body["sorts"] == [{"timestamp": "created_time", "direction": "descending"}]
    assert body["page_size"] == 3
    assert "filter" not in body  # no status → every card
    card = result["cards"][0]
    assert card["title"] == "A card"
    assert card["status"] == "Backlog"
    assert card["tags"] == ["resilience"]
    assert card["url"].startswith("https://notion.so/")


def test_list_cards_maps_logical_status_to_the_notion_option():
    board, client = _board({
        ("GET", f"/databases/{DB_ID}"): {"properties": SCHEMA},
        ("POST", f"/databases/{DB_ID}/query"): {"results": []},
    })
    board.list_cards(status="need_human")
    query = [c for c in client.calls if c[0] == "POST"][0][2]
    assert query["filter"] == {"property": "Status", "select": {"equals": "Need Human"}}


def test_list_cards_page_size_is_clamped_to_notions_limit():
    board, client = _board({("POST", f"/databases/{DB_ID}/query"): {"results": []}})
    board.list_cards(limit=5000)
    assert client.calls[0][2]["page_size"] == 100


def test_missing_database_id_is_a_503_not_a_crash():
    board, _ = _board(config={})
    try:
        board.list_cards()
    except NotionError as exc:
        assert exc.status == 503
        assert "kanban_database_id" in str(exc)
    else:
        raise AssertionError("expected NotionError")


# ── board writes ────────────────────────────────────────────────────────

def test_move_to_need_human_requires_a_comment():
    board, client = _board()
    result = board.move_card("page-1", "need_human")
    assert result["ok"] is False
    assert "comment" in result["error"]
    assert client.calls == []  # nothing touched Notion


def test_move_posts_the_comment_before_changing_status():
    board, client = _board({("GET", f"/databases/{DB_ID}"): {"properties": SCHEMA}})
    result = board.move_card("page-1", "need_human", "blocked on a decision")
    assert result["ok"] is True
    assert result["status"] == "Need Human"
    writes = [c for c in client.calls if c[0] in ("POST", "PATCH")]
    assert writes[0][1] == "/comments"
    assert writes[1][1] == "/pages/page-1"


def test_set_property_rejects_unknown_names_with_the_real_schema():
    board, _ = _board({("GET", f"/databases/{DB_ID}"): {"properties": SCHEMA}})
    result = board.set_property("page-1", "nope", True)
    assert result["ok"] is False
    assert "is_live" in result["available"]


def test_set_property_looks_up_the_type_from_the_live_schema():
    board, client = _board({("GET", f"/databases/{DB_ID}"): {"properties": SCHEMA}})
    assert board.set_property("page-1", "is_live", True)["ok"] is True
    patch = [c for c in client.calls if c[0] == "PATCH"][0][2]
    assert patch["properties"]["is_live"] == {"checkbox": True}


def test_blocker_moves_to_need_human_and_says_no_telegram_was_sent():
    board, client = _board({("GET", f"/databases/{DB_ID}"): {"properties": SCHEMA}})
    result = board.set_blocker("page-1", "missing credentials for the registry")
    assert result["ok"] is True
    assert result["status"] == "Need Human"
    assert "Telegram" in result["note"]
    comment = [c for c in client.calls if c[1] == "/comments"][0][2]
    assert "🚧 Blocker" in comment["rich_text"][0]["text"]["content"]


# ── create / dedup ──────────────────────────────────────────────────────

def test_create_card_writes_only_properties_the_board_actually_has():
    """The monolith wrote a fixed property set and 400'd against any board
    shaped even slightly differently."""
    slim = {"Name": {"type": "title"}, "Status": {"type": "select"}}
    board, client = _board({
        ("GET", f"/databases/{DB_ID}"): {"properties": slim},
        ("POST", "/pages"): {"id": "new-1", "url": "https://notion.so/new-1"},
    })
    result = board.create_card(title="A finding", priority="Alta", source="system-analyst",
                               tags=["docker"], description="short summary", plan="the plan")
    assert result["ok"] is True
    props = [c for c in client.calls if c[1] == "/pages"][0][2]["properties"]
    assert set(props) == {"Name", "Status"}
    assert props["Status"] == {"select": {"name": "Backlog"}}


def test_create_card_maps_ptbr_priority_labels():
    board, client = _board({
        ("GET", f"/databases/{DB_ID}"): {"properties": SCHEMA},
        ("POST", "/pages"): {"id": "new-1"},
        ("POST", f"/databases/{DB_ID}/query"): {"results": []},
    })
    board.create_card(title="x", priority="Alta")
    props = [c for c in client.calls if c[1] == "/pages"][0][2]["properties"]
    assert props["Priority"] == {"select": {"name": "High"}}


def test_create_card_dedupes_by_finding_key_and_bumps_occurrence():
    board, client = _board({
        ("POST", f"/databases/{DB_ID}/query"): {"results": [_page(occ=2)]},
    })
    result = board.create_card(title="same finding", finding_key="resilience:x")
    assert result["is_new"] is False
    assert result["occurrence_count"] == 3
    patch = [c for c in client.calls if c[1] == "/pages/page-1"][0][2]
    assert patch["properties"]["OccurrenceCount"] == {"number": 3}
    callout = [c for c in client.calls if c[1] == "/blocks/page-1/children"][0][2]
    assert "🔁 Re-detectado" in callout["children"][0]["callout"]["rich_text"][0]["text"]["content"]


def test_redetecting_a_done_card_reopens_it_as_a_regression():
    board, client = _board({
        ("POST", f"/databases/{DB_ID}/query"): {"results": [_page(status="Done")]},
    })
    result = board.create_card(title="it came back", finding_key="resilience:x")
    assert result["regression"] is True
    assert result["status"] == "Backlog"
    patch = [c for c in client.calls if c[1] == "/pages/page-1"][0][2]
    assert patch["properties"]["Status"] == {"select": {"name": "Backlog"}}


def test_archived_cards_are_never_reopened():
    board, _ = _board({
        ("POST", f"/databases/{DB_ID}/query"): {"results": [_page(status="Archived")]},
    })
    result = board.create_card(title="old noise", finding_key="resilience:x")
    assert result["skipped"] is True
    assert result["reason"] == "archived"


# ── MCP layer ───────────────────────────────────────────────────────────

def _call(board, name, args):
    return asyncio.run(http_handler.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args}}, board=board))


def test_tools_list_matches_the_handler_table():
    listed = {t["name"] for t in http_handler.TOOLS_SCHEMA}
    assert listed == set(http_handler.HANDLERS)


def test_the_four_agents_platform_tools_are_not_advertised():
    """They were never portable — advertising a tool that can't work is worse
    than not having it."""
    listed = {t["name"] for t in http_handler.TOOLS_SCHEMA}
    assert listed.isdisjoint({"invoke_kanban_agent", "run_ready_cards",
                              "attach_kanban_file", "attach_kanban_presentation"})


def test_page_id_falls_back_to_the_gateway_injected_context():
    assert http_handler._page_id({"_aw_context": {"NOTION_TASK_ID": "ctx-page"}}) == "ctx-page"
    assert http_handler._page_id(
        {"page_id": "explicit", "_aw_context": {"NOTION_TASK_ID": "ctx-page"}}) == "explicit"


def test_mcp_list_returns_cards_as_json_text():
    board, _ = _board({("POST", f"/databases/{DB_ID}/query"): {"results": [_page()]}})
    resp = _call(board, "list_kanban_cards", {"limit": 1})
    assert resp["result"]["isError"] is False
    assert "A card" in resp["result"]["content"][0]["text"]


def test_mcp_reports_a_failed_op_as_an_error_not_a_success():
    board, _ = _board()
    resp = _call(board, "move_kanban_task", {"page_id": "p", "status": "need_human"})
    assert resp["result"]["isError"] is True


def test_mcp_surfaces_notions_own_error_body():
    """object_not_found here means "never shared with the integration" — the
    single most common failure, and unfixable from this side, so the body has
    to reach the caller."""
    class Boom(FakeClient):
        def request(self, method, path, body=None):
            raise NotionError(404, '{"code":"object_not_found"}')

    board = KanbanBoard(Boom(), KanbanConfig(FakeCtx()))
    resp = _call(board, "get_kanban_card", {"page_id": "p"})
    assert resp["result"]["isError"] is True
    assert "object_not_found" in resp["result"]["content"][0]["text"]


def test_qa_status_without_a_card_is_a_soft_ok():
    board, client = _board()
    resp = _call(board, "set_qa_status", {"status": "done"})
    assert resp["result"]["isError"] is False
    assert client.calls == []


def test_unknown_tool_is_an_error_not_a_crash():
    board, _ = _board()
    resp = _call(board, "nope", {})
    assert resp["result"]["isError"] is True
