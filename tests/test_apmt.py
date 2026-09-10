"""Tests for notion_app/apmt.py and the three route behaviours it added
(Kanban architecture:notion-token-per-tenant-ap-mt-step1):

* ``POST /settings`` pushes, and a failed push does NOT fail the save;
* ``POST /logout`` deletes remotely FIRST and refuses to report success when
  that delete failed — while still letting a workspace with no relay log out;
* ``POST /apmt/sync`` re-pushes only on fingerprint drift.

No network: the autouse guard in conftest.py already blocks ``apmt._call``,
and the tests that exercise the sync path replace it with a recorder.

Run: python3 -m pytest tests/test_apmt.py
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notion_app import apmt as apmt_mod, routes  # noqa: E402

from test_routes import FakeCtx  # noqa: E402  — one fake ctx, not two (rootless
                                 # test package: pytest puts tests/ on sys.path)


class Relay:
    """Records what would have gone to the runners app, and answers with
    whatever the test set up. ``fail`` raises the given exception instead."""

    def __init__(self, *, remote_fingerprint: str = "", fail: Exception | None = None):
        self.remote_fingerprint = remote_fingerprint
        self.fail = fail
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls.append((method, path, body))
        if self.fail is not None:
            raise self.fail
        if method == "GET":
            return {"configured": bool(self.remote_fingerprint),
                    "token_fingerprint": self.remote_fingerprint}
        if method == "POST":
            self.remote_fingerprint = apmt_mod.token_fingerprint((body or {}).get("token"))
            return {"configured": True, "token_fingerprint": self.remote_fingerprint}
        self.remote_fingerprint = ""
        return {"deleted": True, "configured": False}

    @property
    def methods(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def client_ctx():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeCtx(package_dir=tmp)
        yield TestClient(routes.build_routes(ctx)), ctx


def _relay(block_apmt_network, relay: Relay) -> Relay:
    block_apmt_network.setattr(apmt_mod, "_call", relay)
    return relay


# --- fingerprint ------------------------------------------------------------


def test_fingerprint_is_plain_sha256_and_empty_for_no_token():
    assert apmt_mod.token_fingerprint("ntn_abc") == hashlib.sha256(b"ntn_abc").hexdigest()
    assert apmt_mod.token_fingerprint("") == ""
    assert apmt_mod.token_fingerprint(None) == ""


# --- push on save -----------------------------------------------------------


def test_saving_a_token_pushes_it(client_ctx, block_apmt_network):
    client, ctx = client_ctx
    relay = _relay(block_apmt_network, Relay())

    r = client.post("/settings", json={"notion_token": "ntn_saved"})
    assert r.status_code == 200
    assert r.json()["apmt"]["pushed"] is True
    assert relay.calls == [("POST", "/notion-token", {"token": "ntn_saved"})]
    assert ctx.secrets.read("notion_token") == "ntn_saved"


def test_a_failed_push_does_not_fail_the_save(client_ctx, block_apmt_network):
    """The local save is the source of truth and already succeeded; the 360s
    reconcile repairs the remote copy. Failing the save here would make a
    briefly-unreachable control plane look like a broken Notion login."""
    client, ctx = client_ctx
    _relay(block_apmt_network, Relay(fail=apmt_mod.ApmtSyncError("AP-MT is down")))

    r = client.post("/settings", json={"notion_token": "ntn_saved"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["apmt"] == {"pushed": False, "reason": "AP-MT is down"}
    assert ctx.secrets.read("notion_token") == "ntn_saved"


# --- delete on logout -------------------------------------------------------


def test_logout_deletes_remotely_first(client_ctx, block_apmt_network):
    client, ctx = client_ctx
    relay = _relay(block_apmt_network, Relay(remote_fingerprint="anything"))
    client.post("/settings", json={"notion_token": "ntn_bye"})
    relay.calls.clear()

    r = client.post("/logout")
    assert r.status_code == 200
    assert r.json()["logged_in"] is False
    assert relay.methods == ["DELETE"]
    assert ctx.secrets.read("notion_token") is None


def test_logout_fails_visibly_when_the_remote_delete_fails(client_ctx, block_apmt_network):
    """The point of the whole card: "I disconnected Notion" must not be
    reported while a working token is still live in the control plane."""
    client, ctx = client_ctx
    relay = _relay(block_apmt_network, Relay())
    client.post("/settings", json={"notion_token": "ntn_stuck"})
    relay.fail = apmt_mod.ApmtSyncError("AP-MT refused the call (500)")

    r = client.post("/logout")
    assert r.status_code == 502
    body = r.json()
    assert body["ok"] is False
    assert body["logged_in"] is True
    assert "still live there" in body["error"]
    # The local token stays: it is the only thing that still proves what has
    # to be revoked, and the retry has to be able to succeed.
    assert ctx.secrets.read("notion_token") == "ntn_stuck"


def test_logout_proceeds_when_there_is_no_relay_at_all(client_ctx):
    """Blocked by the autouse guard == the same ApmtNotConfigured a workspace
    without aw-app-agents-platform-runners reports. That must not make logout
    impossible."""
    client, ctx = client_ctx
    ctx.secrets.write("notion_token", "ntn_local_only")

    r = client.post("/logout")
    assert r.status_code == 200
    assert r.json()["logged_in"] is False
    assert r.json()["apmt"]["deleted"] is False
    assert ctx.secrets.read("notion_token") is None


# --- reconcile --------------------------------------------------------------


def test_reconcile_sends_nothing_when_the_fingerprints_match(client_ctx, block_apmt_network):
    client, ctx = client_ctx
    ctx.secrets.write("notion_token", "ntn_same")
    relay = _relay(block_apmt_network,
                   Relay(remote_fingerprint=apmt_mod.token_fingerprint("ntn_same")))

    r = client.post("/apmt/sync")
    assert r.json() == {"reconciled": True, "changed": False, "reason": "fingerprints match"}
    assert relay.methods == ["GET"]  # read only — the token never left


def test_reconcile_repushes_after_a_rotation(client_ctx, block_apmt_network):
    """A token rotated locally while AP-MT was unreachable: the next tick
    notices the divergence and pushes the new one."""
    client, ctx = client_ctx
    ctx.secrets.write("notion_token", "ntn_rotated")
    relay = _relay(block_apmt_network,
                   Relay(remote_fingerprint=apmt_mod.token_fingerprint("ntn_old")))

    r = client.post("/apmt/sync")
    assert r.json() == {"reconciled": True, "changed": True, "action": "pushed"}
    assert relay.calls[-1] == ("POST", "/notion-token", {"token": "ntn_rotated"})
    assert relay.remote_fingerprint == apmt_mod.token_fingerprint("ntn_rotated")

    # ...and the tick after that is a no-op again.
    assert client.post("/apmt/sync").json()["changed"] is False


def test_reconcile_deletes_a_remote_copy_with_no_local_token(client_ctx, block_apmt_network):
    """Repairs a logout whose remote delete failed AFTER the local secret was
    gone — impossible through /logout today (it deletes remotely first), but
    reachable by a secret cleared any other way, and cheap to close."""
    client, _ctx = client_ctx
    relay = _relay(block_apmt_network, Relay(remote_fingerprint="stale-fingerprint"))

    r = client.post("/apmt/sync")
    assert r.json() == {"reconciled": True, "changed": True, "action": "deleted"}
    assert relay.methods == ["GET", "DELETE"]


def test_reconcile_reports_an_unreachable_relay_without_raising(client_ctx, block_apmt_network):
    """It runs in a watchdog — an exception here is a stack trace every six
    minutes that nobody reads."""
    client, ctx = client_ctx
    ctx.secrets.write("notion_token", "ntn_x")
    _relay(block_apmt_network, Relay(fail=apmt_mod.ApmtSyncError("relay unreachable")))

    r = client.post("/apmt/sync")
    assert r.json() == {"reconciled": False, "changed": False, "reason": "relay unreachable"}
