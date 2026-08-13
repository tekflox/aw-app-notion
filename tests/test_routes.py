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
        assert resp.json() == {"ok": True, "logged_in": False, "configured": False}
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
