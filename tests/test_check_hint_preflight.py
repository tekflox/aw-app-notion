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


def _closed_port() -> int:
    """A TCP port nothing is listening on, right now, on this machine.

    Binding to port 0 and closing immediately hands back a port the kernel
    just confirmed was free. Hardcoding one does not work: this suite also
    runs on the self-hosted release runner, which already had 9123 bound —
    half the reason the dead-host cases below used to pass locally and fail
    in CI for a reason that had nothing to do with the code under test.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _missing_path(tmp_path) -> str:
    """A path shaped like the incident's, guaranteed absent on ANY machine.

    The incident these tests encode cited
    ``/opt/agentic-workspace/repos/aw-backend/src/api/pg_db.py`` — the
    retired monolith checkout. Spelling that literal into a fixture couples
    the test to one machine's filesystem: it is absent in the app container
    (where the preflight actually runs) but still present on the bare-metal
    release runner, so the assertion tested the runner, not the code.
    """
    p = tmp_path / "repos" / "aw-backend" / "src" / "api" / "pg_db.py"
    assert not p.exists(), "fixture must be absent for this test to mean anything"
    return str(p)


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


def test_hint_referencing_only_a_missing_path_is_unverifiable(tmp_path):
    missing = _missing_path(tmp_path)
    result = preflight_check_hint(f"grep -q 'pg_advisory_lock' {missing}")
    assert result["verdict"] == "UNVERIFIABLE"
    assert missing in result["reasons"][0]


def test_hint_with_one_missing_and_one_real_path_is_ok_but_flags_it(tmp_path):
    real_file = tmp_path / "marker.txt"
    real_file.write_text("x")
    hint = f"test -f /opt/definitely-does-not-exist-xyz && test -f {real_file}"
    result = preflight_check_hint(hint)
    assert result["verdict"] == "OK"
    assert any("does-not-exist" in r for r in result["reasons"])


def test_hint_referencing_an_unreachable_host_port_is_unverifiable():
    result = preflight_check_hint(
        f"curl -sf http://127.0.0.1:{_closed_port()}/health", connect_timeout=0.2)
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


def test_real_incident_shape_curl_negation_over_dead_host(tmp_path):
    # The exact card-cited false-green: `! curl ... || grep ...` where the
    # curl target is unreachable, so `!` flips it to "success" without the
    # grep ever running. Preflight can't see the `!`/`||` control flow, but
    # it CAN see that EVERY concrete target in the hint is dead — which is
    # what makes both halves of this fixture have to be dead by construction.
    hint = (f'! curl -s "http://127.0.0.1:{_closed_port()}/api/health" >/dev/null '
            f'|| grep -q "pg_advisory_lock" {_missing_path(tmp_path)}')
    result = preflight_check_hint(hint, connect_timeout=0.2)
    assert result["verdict"] == "UNVERIFIABLE"
