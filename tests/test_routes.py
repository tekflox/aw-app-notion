"""TestClient coverage for notion_app/routes.py's build_routes(ctx) — no
framework runtime needed, just a minimal fake ctx (secrets facade +
package_dir) matching aw-app-git's tests/test_plugin_routes.py pattern.

Run: .venv/aw/bin/python -m pytest tests/test_routes.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notion_app import mcp_config, routes  # noqa: E402


class FakeSecrets:
    def __init__(self):
        self.store: dict[str, str] = {}

    def read(self, key):
        return self.store.get(key)

    def write(self, key, value):
        self.store[key] = value
        return {"key": key, "written": True}

    def delete(self, key):
        removed = key in self.store
        self.store.pop(key, None)
        return {"key": key, "deleted": removed}

    def keys(self):
        return list(self.store)


class FakeCtx:
    def __init__(self, package_dir: str):
        self.secrets = FakeSecrets()
        self.config = {}
        self.package_dir = package_dir


def _client(tmp_path):
    ctx = FakeCtx(package_dir=str(tmp_path))
    return TestClient(routes.build_routes(ctx)), ctx


# ── GET /kanban/cards-need-attention — sync REST wrapper over
# KanbanBoard.list_cards_need_attention, for the kanban-sweep dag Workflow's
# http node to gate on (card 4165bf3b-9510-81c8). ──────────────────────────

NEED_ATTENTION_DB_ID = "3645bf3b-9510-80d9-af78-d085fc94d571"
NEED_ATTENTION_SCHEMA = {"Status": {"type": "select"}, "Priority": {"type": "select"}}


def _need_attention_page(page_id, *, priority="High", status="In Progress",
                         created="2026-11-10T10:00:00.000Z"):
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "created_time": created,
        "last_edited_time": created,
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": page_id}]},
            "Status": {"type": "select", "select": {"name": status}},
            "Priority": {"type": "select", "select": {"name": priority}},
        },
    }


class _FakeNotionResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_notion_api(monkeypatch, pages):
    """Fake the real Notion HTTP calls KanbanBoard.list_cards_need_attention
    makes through NotionClient (schema GET + a single-page query POST) —
    same pattern as test_bot_translates_users_me_into_workspace_and_integration_ids,
    just with two distinct endpoints instead of one."""
    from notion_app.kanban import client as client_mod

    def fake_urlopen(req, timeout=None):
        if req.get_method() == "GET":
            return _FakeNotionResp({"properties": NEED_ATTENTION_SCHEMA})
        return _FakeNotionResp({"results": pages, "has_more": False})

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake_urlopen)
    # Avoid the real ~0.34s inter-request pacing sleep slowing the suite down.
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)


def _configured_client(tmp_path):
    client, ctx = _client(tmp_path)
    ctx.secrets.write("notion_token", "ntn_test123")
    ctx.config = {"kanban_database_id": NEED_ATTENTION_DB_ID}
    return client, ctx


def test_cards_need_attention_runs_off_the_event_loop(monkeypatch):
    """Code-review finding (PR #7): this is the only GET route whose board
    call paginates multiple paced/retried Notion requests and is polled
    periodically (the kanban-sweep dag Workflow's http node), so running it
    synchronously inside the handler would stall this in-process app's
    shared event loop on every poll. Assert the handler actually goes
    through run_in_threadpool, not just that the response still works."""
    calls = []
    real_run_in_threadpool = routes.run_in_threadpool

    async def spy(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return await real_run_in_threadpool(fn, *args, **kwargs)

    monkeypatch.setattr(routes, "run_in_threadpool", spy)

    with tempfile.TemporaryDirectory() as tmp:
        client, _ctx = _client(Path(tmp))  # unconfigured is enough — no Notion call needed
        resp = client.get("/kanban/cards-need-attention")
        assert resp.status_code == 503
    assert len(calls) == 1


def test_cards_need_attention_is_503_not_200_when_unconfigured():
    """The workflow gate must be able to tell "no work" from "check broken"
    — an unconfigured board has to surface as a non-2xx, never a 200 with
    has_work=false (card 4165bf3b-9510-81c8's explicit requirement)."""
    with tempfile.TemporaryDirectory() as tmp:
        client, _ctx = _client(Path(tmp))  # no kanban_database_id set
        resp = client.get("/kanban/cards-need-attention")
        assert resp.status_code == 503
        assert resp.json()["ok"] is False


def test_cards_need_attention_has_work_true_with_cards(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        client, _ctx = _configured_client(Path(tmp))
        _patch_notion_api(monkeypatch, [_need_attention_page("p1"), _need_attention_page("p2")])

        resp = client.get("/kanban/cards-need-attention")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_work"] is True
        assert {c["page_id"] for c in body["cards"]} == {"p1", "p2"}
        assert body["counts"] == {"count": 2, "scanned": 2, "truncated": False}


def test_cards_need_attention_has_work_false_with_no_cards(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        client, _ctx = _configured_client(Path(tmp))
        _patch_notion_api(monkeypatch, [])

        resp = client.get("/kanban/cards-need-attention")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_work"] is False
        assert body["cards"] == []
        assert body["counts"] == {"count": 0, "scanned": 0, "truncated": False}


def test_cards_need_attention_limit_clamp_passthrough(monkeypatch):
    """The route's ``?limit=`` just passes through to the board's own
    1-25 clamp — no new clamping logic in the route itself."""
    with tempfile.TemporaryDirectory() as tmp:
        client, _ctx = _configured_client(Path(tmp))
        pages = [_need_attention_page(f"p{i}", created=f"2026-11-{i % 28 + 1:02d}T00:00:00.000Z")
                 for i in range(30)]
        _patch_notion_api(monkeypatch, pages)

        resp = client.get("/kanban/cards-need-attention", params={"limit": 999})
        assert len(resp.json()["cards"]) == 25  # clamped to 25, not the full 30

        resp = client.get("/kanban/cards-need-attention", params={"limit": 0})
        assert len(resp.json()["cards"]) == 1  # clamped up to 1, never 0


def test_cards_need_attention_matches_the_mcp_tool_for_the_same_fixture():
    """Route and MCP tool (`list_kanban_cards_need_attention` /
    `_h_need_attention`) must wrap the exact same
    KanbanBoard.list_cards_need_attention — same cards for the same fixture,
    per the card's explicit parity requirement. Uses the FakeClient fixture
    test_kanban.py already exercises this method against, so this is a real
    cross-entry-point comparison rather than two copies of the same mock."""
    from notion_app.kanban.cards import KanbanBoard
    from notion_app.kanban.config import KanbanConfig
    from notion_app.mcp import http_handler

    class _FakeCtx:
        def __init__(self):
            self.config = {"kanban_database_id": NEED_ATTENTION_DB_ID}

    class _FakeClient:
        configured = True

        def __init__(self, pages):
            self._pages = pages

        def database_schema(self, _db_id):
            return NEED_ATTENTION_SCHEMA

        def query_database(self, _db_id, _body):
            return {"results": self._pages, "has_more": False}

    pages = [_need_attention_page("p1"), _need_attention_page("p2")]
    board = KanbanBoard(_FakeClient(pages), KanbanConfig(_FakeCtx()))
    mcp_result = http_handler._h_need_attention(board, {"limit": 10})

    with tempfile.TemporaryDirectory() as tmp:
        import pytest
        with pytest.MonkeyPatch.context() as monkeypatch:
            client, _ctx = _configured_client(Path(tmp))
            _patch_notion_api(monkeypatch, pages)
            route_body = client.get("/kanban/cards-need-attention").json()

    assert [c["page_id"] for c in route_body["cards"]] == [c["page_id"] for c in mcp_result["cards"]]
    assert route_body["counts"]["count"] == mcp_result["count"]
    assert route_body["counts"]["scanned"] == mcp_result["scanned"]


def test_status_without_token():
    with tempfile.TemporaryDirectory() as tmp:
        client, _ctx = _client(Path(tmp))
        resp = client.get("/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["logged_in"] is False
        assert body["configured"] is False
        assert body["mcp_server_enabled"] is False
        # Kanban reports separately: no board id is a different problem from
        # no token, and the UI has to be able to tell them apart.
        assert body["kanban"]["configured"] is False
        assert body["kanban"]["statuses"]["need_human"] == "Need Human"


def test_save_settings_writes_secret_and_mcp_json():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client, ctx = _client(tmp_path)

        resp = client.post("/settings", json={"notion_token": "ntn_test123"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["logged_in"] is True
        assert body["mcp_server_enabled"] is True
        assert ctx.secrets.read("notion_token") == "ntn_test123"

        mcp_json_path = tmp_path / "mcp.json"
        assert mcp_json_path.is_file()
        doc = json.loads(mcp_json_path.read_text())
        assert doc["mcpServers"]["notion"]["env"]["NOTION_TOKEN"] == "ntn_test123"

        status = client.get("/status").json()
        assert status["logged_in"] is True


def test_save_settings_rejects_empty_token():
    with tempfile.TemporaryDirectory() as tmp:
        client, _ctx = _client(Path(tmp))
        resp = client.post("/settings", json={"notion_token": "  "})
        assert resp.status_code == 200
        assert "error" in resp.json()


def test_logout_clears_secret_and_disables_mcp_server():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client, ctx = _client(tmp_path)
        client.post("/settings", json={"notion_token": "ntn_test123"})

        resp = client.post("/logout")
        assert resp.status_code == 200
        body = resp.json()
        # `apmt` (the AP-MT copy's delete result) is also in the body now —
        # asserted in test_apmt.py, which owns that behaviour; here the point
        # is still just that the local logout worked.
        assert {k: body[k] for k in ("ok", "logged_in", "configured")} == {
            "ok": True, "logged_in": False, "configured": False}
        assert ctx.secrets.read("notion_token") is None

        doc = json.loads((tmp_path / "mcp.json").read_text())
        assert doc["mcpServers"] == {}


def test_mcp_json_endpoint_mirrors_disk_state():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client, _ctx = _client(tmp_path)
        client.post("/settings", json={"notion_token": "ntn_abc"})

        resp = client.get("/mcp.json")
        assert resp.json() == json.loads((tmp_path / "mcp.json").read_text())


def test_build_mcp_servers_empty_without_token():
    assert mcp_config.build_mcp_servers(None) == {}
    assert mcp_config.build_mcp_servers("") == {}


def test_build_mcp_servers_shape_with_token():
    servers = mcp_config.build_mcp_servers("ntn_xyz")
    assert servers["notion"]["command"] == "npx"
    assert servers["notion"]["args"] == ["-y", "@notionhq/notion-mcp-server"]
    assert servers["notion"]["env"]["NOTION_TOKEN"] == "ntn_xyz"


def test_build_mcp_servers_also_advertises_kanban():
    """Both servers appear or neither does — the kanban one is useless
    without a token too, so it must not be advertised on its own."""
    servers = mcp_config.build_mcp_servers("ntn_xyz", port=9030)
    assert set(servers) == {"notion", "aw-kanban"}
    kanban = servers["aw-kanban"]
    assert kanban["type"] == "http"
    assert kanban["url"].endswith(":9030/api/apps/notion/mcp")


# ── GET /bot (Kanban "auto-criar sweep de Ready cards + automatizar/
# verificar webhook do Notion") — aw-app-agents-platform-runners' loopback
# read of this workspace's Notion bot identity, since it cannot read this
# app's own token. ─────────────────────────────────────────────────────────


def test_bot_without_a_token_is_409():
    with tempfile.TemporaryDirectory() as tmp:
        client, _ctx = _client(Path(tmp))
        resp = client.get("/bot")
        assert resp.status_code == 409


def test_bot_translates_users_me_into_workspace_and_integration_ids(monkeypatch):
    from notion_app.kanban import client as client_mod

    class FakeResp:
        status = 200

        def read(self):
            return json.dumps({
                "id": "bot-id-123",
                "object": "user",
                "type": "bot",
                "bot": {"workspace_id": "ws-abc", "workspace_name": "AW Dev"},
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        client, ctx = _client(Path(tmp))
        ctx.secrets.write("notion_token", "ntn_test123")
        resp = client.get("/bot")

    assert resp.status_code == 200
    assert resp.json() == {"integration_id": "bot-id-123", "workspace_id": "ws-abc",
                           "workspace_name": "AW Dev"}
    assert seen[0].endswith("/users/me")


def test_bot_surfaces_notions_own_error_status(monkeypatch):
    """404/401 from Notion has to reach the caller as-is — same
    "surface Notion's own status" contract every other _kanban-wrapped
    route here already keeps (see test_mcp_surfaces_notions_own_error_body
    in test_kanban.py)."""
    import urllib.error

    from notion_app.kanban import client as client_mod

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", boom)

    with tempfile.TemporaryDirectory() as tmp:
        client, ctx = _client(Path(tmp))
        ctx.secrets.write("notion_token", "ntn_test123")
        resp = client.get("/bot")

    assert resp.status_code == 401
