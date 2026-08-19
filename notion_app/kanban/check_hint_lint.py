"""Static risk check for a Kanban card's ``check_hint`` — catch the shapes of
false-green documented (and re-detected 4 times) on
``tooling:checkhint-false-green-on-missing-target``.

This is deliberately NOT an attempt to execute or fully validate a hint —
the hint is written for and run inside an *agent* container a day later
(see the ``aw-system-analyst`` skill's CheckHint constraints), a
filesystem/network namespace this app's own container does not share. What
this module CAN do, entirely offline, is refuse the specific textual shapes
that have already produced a false "resolved" three separate times:

* ``! curl``/``! wget``/``! grep`` — negating a command whose "it failed"
  and "the target doesn't exist" exit codes collapse into the same
  success, so an unreachable endpoint or a missing file reads as "checked
  and clean" instead of "never actually checked".
* ``glob.glob(...)`` — an empty/missing directory and a directory with zero
  matching files return the identical ``[]``; code that treats "0 results"
  as "resolved" cannot tell them apart.
* a bare no-op (``true``, ``:``, ``exit 0``, or an empty string) — verifies
  nothing by construction.
* a hardcoded reference to ``/opt/agentic-workspace`` — the retired
  monolith checkout, which does not exist on this workspace, so a hint
  built against it is green by absence, not by verification.

Written at CARD-CREATE/UPDATE time (``KanbanBoard.create_card`` /
``set_property``) rather than at saneamento-run time, because saneamento is
an LLM re-reading a skill's prose each run — proven (4 occurrences) not to
reliably catch these on its own — while this runs deterministically, every
time, with no reliance on the reader remembering the rule.
"""
from __future__ import annotations

import re

_TRIVIAL_HINTS = {"true", ":", "exit 0", "exit(0)"}

_RISK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"!\s*curl\b"),
     "negates curl — an unreachable endpoint (connection refused/timeout) reads "
     "as success instead of 'never actually checked'"),
    (re.compile(r"!\s*wget\b"),
     "negates wget — same false-green shape as curl: unreachable reads as success"),
    (re.compile(r"!\s*grep\b"),
     "negates grep — grep's 'no match' (exit 1) and 'file not found' (exit 2) "
     "collapse into the same negated success; a missing file reads as 'clean'"),
    (re.compile(r"\bglob\.glob\("),
     "glob.glob() over a directory that doesn't exist returns [] — identical to "
     "a directory that exists with zero matches; '0 results' cannot tell them apart"),
    (re.compile(r"/opt/agentic-workspace\b"),
     "points at the retired monolith checkout, which does not exist on this "
     "workspace — this hint is green by absence, not by verification"),
]


def lint_check_hint(hint: str) -> list[str]:
    """Reasons this hint risks a false-green, or ``[]`` if none matched.

    Not exhaustive — it catches the documented recurring shapes, not every
    way a hint can lie. A clean result here is not proof the hint is good;
    a non-empty result IS proof it matches a shape that has already
    produced a false "resolved" on this board.
    """
    text = (hint or "").strip()
    if not text:
        return ["check_hint is empty — Phase 1 saneamento cannot verify anything "
                "and must leave the card open forever"]
    if text in _TRIVIAL_HINTS:
        return [f"hint is a no-op ({text!r}) — always exits 0, verifies nothing"]
    return [msg for pattern, msg in _RISK_PATTERNS if pattern.search(text)]
