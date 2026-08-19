"""Coverage for check_hint_preflight — the runtime half of
tooling:checkhint-false-green-on-missing-target (check_hint_lint.py is the
write-time half).

Run: .venv/aw/bin/python -m pytest tests/test_check_hint_preflight.py
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notion_app.kanban.check_hint_preflight import preflight_check_hint  # noqa: E402


def test_empty_hint_is_unverifiable():
    result = preflight_check_hint("")
    assert result["verdict"] == "UNVERIFIABLE"


def test_hint_with_no_literal_target_is_ok():
    # No absolute path, no host:port — nothing this module can check, so it
    # must not manufacture a false UNVERIFIABLE out of thin air.
    result = preflight_check_hint("grep -q 'foo' <<< \"$INPUT\"")
    assert result["verdict"] == "OK"
    assert result["checked"] == {"paths": {}, "hosts": {}}


def test_hint_referencing_a_real_path_is_ok(tmp_path):
    real_file = tmp_path / "marker.txt"
    real_file.write_text("x")
    result = preflight_check_hint(f"grep -q x {real_file}")
    assert result["verdict"] == "OK"
    assert result["checked"]["paths"] == {str(real_file): True}


def test_hint_referencing_only_a_missing_path_is_unverifiable():
    result = preflight_check_hint(
        "grep -q 'pg_advisory_lock' /opt/agentic-workspace/repos/aw-backend/src/api/pg_db.py")
    assert result["verdict"] == "UNVERIFIABLE"
    assert "agentic-workspace" in result["reasons"][0]


def test_hint_with_one_missing_and_one_real_path_is_ok_but_flags_it(tmp_path):
    real_file = tmp_path / "marker.txt"
    real_file.write_text("x")
    hint = f"test -f /opt/definitely-does-not-exist-xyz && test -f {real_file}"
    result = preflight_check_hint(hint)
    assert result["verdict"] == "OK"
    assert any("does-not-exist" in r for r in result["reasons"])


def test_hint_referencing_an_unreachable_host_port_is_unverifiable():
    # A high ephemeral port is essentially never listening in a test sandbox;
    # using a short timeout keeps this fast regardless.
    result = preflight_check_hint(
        "curl -sf http://127.0.0.1:59999/health", connect_timeout=0.2)
    assert result["verdict"] == "UNVERIFIABLE"


def test_hint_referencing_a_live_host_port_is_ok():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        result = preflight_check_hint(f"curl -sf http://127.0.0.1:{port}/health")
        assert result["verdict"] == "OK"
        assert result["checked"]["hosts"] == {f"127.0.0.1:{port}": True}
    finally:
        srv.close()


def test_real_incident_shape_curl_negation_over_dead_host():
    # The exact card-cited false-green: `! curl ... || grep ...` where the
    # curl target is unreachable, so `!` flips it to "success" without the
    # grep ever running. Preflight can't see the `!`/`||` control flow, but
    # it CAN see that the only concrete target in the hint is dead.
    hint = '! curl -s "http://127.0.0.1:9123/api/health" >/dev/null || grep -q "pg_advisory_lock" /opt/agentic-workspace/repos/aw-backend/src/api/pg_db.py'
    result = preflight_check_hint(hint, connect_timeout=0.2)
    assert result["verdict"] == "UNVERIFIABLE"
