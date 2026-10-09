"""Coverage for notion_app/plugin.py::NotionAppPlugin.activate's boot-time
apmt reconcile (Kanban architecture:decommission-aw-app-notion-into-ap-mt,
comment 7.A.i) — "ao instalar" has to push the token (and board config)
immediately rather than waiting out the 360s reconcile tick.

Run: python3 -m pytest tests/test_plugin.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notion_app import apmt as apmt_mod  # noqa: E402
from notion_app.plugin import NotionAppPlugin  # noqa: E402

from test_apmt import Relay  # noqa: E402
from test_routes import FakeCtx  # noqa: E402


class _FakeRoutes:
    def register(self, app) -> None:
        pass


def _ctx(tmp_path, *, token: str | None = None, config: dict | None = None) -> FakeCtx:
    ctx = FakeCtx(package_dir=str(tmp_path))
    ctx.routes = _FakeRoutes()
    if token:
        ctx.secrets.write("notion_token", token)
    if config:
        ctx.config = config
    return ctx


def test_activate_reconciles_the_token_and_board_config_on_boot(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(Path(tmp), token="ntn_boot",
                  config={"kanban_database_id": "db_boot"})
        relay = Relay()
        monkeypatch.setattr(apmt_mod, "_call", relay)

        asyncio.run(NotionAppPlugin().activate(ctx))

        assert relay.methods == ["GET", "POST"]
        pushed = relay.calls[-1]
        assert pushed[0] == "POST"
        assert pushed[2]["token"] == "ntn_boot"
        assert pushed[2]["kanban_database_id"] == "db_boot"


def test_activate_does_not_raise_when_apmt_is_unreachable(monkeypatch):
    """Fail-open, same contract as every other boot step in this app."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(Path(tmp), token="ntn_boot")
        monkeypatch.setattr(
            apmt_mod, "_call",
            Relay(fail=apmt_mod.ApmtSyncError("AP-MT is down")))

        asyncio.run(NotionAppPlugin().activate(ctx))  # must not raise
