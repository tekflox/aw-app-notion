"""Runtime preflight for a Kanban card's ``check_hint`` — the missing half of
``check_hint_lint.py`` (``tooling:checkhint-false-green-on-missing-target``).

``lint_check_hint`` catches known-bad SHAPES at card create/update time.
What it cannot catch is drift: a hint that was perfectly fine when written
later points at a path that got renamed, a repo that got retired, or a host
that stopped listening — and then exits 0 for the wrong reason (the target
doesn't exist rather than the issue being fixed). Documented live 4 times on
this exact finding_key, twice by an agent that happened to notice and NOT
trust the exit code, which is the failure mode this exists to stop relying
on: a human/agent's carefulness is not a control.

Deliberately does NOT execute the hint itself — that stays the caller's own
Bash tool, unchanged. This only answers "does what the hint references
actually exist/listen", read-only and offline apart from a short TCP
connect probe, so a saneamento pass can get a mechanical UNVERIFIABLE
instead of re-deriving the same judgment call from prose every run.
"""
from __future__ import annotations

import os
import re
import socket

_ABS_PATH_RE = re.compile(r"(?<![\w.:-])(/[\w./-]{2,})")
_HOST_PORT_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}|localhost)[:](\d{2,5})\b")
_TRAILING_PUNCT = ".,;:)'\"”’"
# Shell-idiom paths that exist on every POSIX box and carry no information
# about whether THIS hint's actual target is present — `>/dev/null 2>&1` is
# in roughly every hint ever written and would otherwise make an all-dead
# hint look partially-alive.
_ALWAYS_IGNORE_PATH_PREFIXES = ("/dev/",)


def _candidate_paths(text: str) -> list[str]:
    out = set()
    for m in _ABS_PATH_RE.finditer(text):
        p = m.group(1).rstrip(_TRAILING_PUNCT)
        # Require at least two '/' so a bare flag-looking fragment like
        # '/tmp' from an unrelated word, or a single-segment root, doesn't
        # get treated as a meaningful target on its own.
        if p.count("/") < 2:
            continue
        if p.startswith(_ALWAYS_IGNORE_PATH_PREFIXES):
            continue
        out.add(p)
    return sorted(out)


def _candidate_host_ports(text: str) -> list[tuple[str, int]]:
    out = set()
    for m in _HOST_PORT_RE.finditer(text):
        out.add((m.group(1), int(m.group(2))))
    return sorted(out)


def preflight_check_hint(hint: str, *, connect_timeout: float = 1.5) -> dict:
    """Best-effort existence/reachability check for every filesystem path
    and ``host:port`` literal referenced in ``hint``.

    Returns ``{"verdict": "OK" | "UNVERIFIABLE", "reasons": [...], "checked":
    {"paths": {...}, "hosts": {...}}}``.

    ``UNVERIFIABLE`` only fires when EVERY extractable target came back
    missing/unreachable — a hint that checks "path A is gone AND path B
    exists" is a legitimate pattern and must not be flagged just because
    one half is absent by design. A hint with no extractable literal (piped
    input, a Python snippet with no hardcoded path, an env var) can't be
    preflighted this way at all and comes back ``OK`` — no checkable target
    is not evidence of anything, unlike a checkable target that's absent.
    """
    text = (hint or "").strip()
    if not text:
        return {"verdict": "UNVERIFIABLE", "reasons": ["check_hint is empty"],
                "checked": {"paths": {}, "hosts": {}}}

    path_status = {p: os.path.exists(p) for p in _candidate_paths(text)}
    host_status = {}
    for host, port in _candidate_host_ports(text):
        try:
            with socket.create_connection((host, port), timeout=connect_timeout):
                host_status[f"{host}:{port}"] = True
        except OSError:
            host_status[f"{host}:{port}"] = False

    checked = {"paths": path_status, "hosts": host_status}
    combined = {**path_status, **host_status}
    if not combined:
        return {"verdict": "OK", "reasons": [], "checked": checked}

    missing = sorted(k for k, ok in combined.items() if not ok)
    if len(missing) == len(combined):
        return {"verdict": "UNVERIFIABLE",
                "reasons": [f"every target this hint references is missing/unreachable: "
                            f"{', '.join(missing)} — its exit code proves nothing"],
                "checked": checked}
    return {"verdict": "OK",
            "reasons": [f"{m} missing/unreachable (other referenced targets are fine)"
                        for m in missing],
            "checked": checked}
