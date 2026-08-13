"""``aw-workspace-cli notion-sync`` — this app's own CLI command.

Replaces the monolith's ``./aw notion-sync``. Auto-discovered by
aw-workspace-cli from this app's installed directory
(``<apps_root>/notion/commands/``, since this file lives at ``commands/`` in
this repo's root — see aw-workspace's ``src/cli/discovery.py``, which loads
every ``<apps_root>/<slug>/commands/*.py`` exposing
``COMMAND``/``DESCRIPTION``/``run``).

Unlike aw-app-remote-host-cli's command, this one does **not** import the
app's package and run the work locally. It can't: the Notion token lives in
this app's zero-knowledge secret store, readable only through ``ctx.secrets``
inside the running workspace process. So the CLI is a thin client over
``POST /api/apps/notion/sync``, authenticating with the workspace API key the
same way ``aw-workspace-cli marketplace`` does. The sync engine itself
(``notion_app/sync.py``) stays the single source of truth and is equally
reachable from an agent, the UI, or curl.

Two mirrors, one command:

* ``notion/notes/``            — child pages under ``sync_root_page_id``
* ``notion/kanban/<status>/``  — every Kanban card, filed by its status

Usage:
    aw-workspace-cli notion-sync                      # both, changed only
    aw-workspace-cli notion-sync --force              # re-render everything
    aw-workspace-cli notion-sync --notes-only
    aw-workspace-cli notion-sync --kanban-only
    aw-workspace-cli notion-sync --bidirectional      # override the saved setting
    aw-workspace-cli notion-sync --no-bidirectional   # pull-only
    aw-workspace-cli notion-sync --no-rebuild         # skip the KB reindex
    aw-workspace-cli notion-sync --status             # what the last sync did
"""
from __future__ import annotations

import argparse
import sys
import time

COMMAND = "notion-sync"
DESCRIPTION = "Mirror Notion notes + Kanban cards into the knowledge base"

PROG = "aw-workspace-cli notion-sync"

# The sync runs as a background job on the server (202 + poll), because the
# BYOD tunnel drops a long-held request and answers "502 workspace offline"
# while the work is still running fine — see notion_app/job.py. So no request
# this command makes is ever long; only the total wait is.
POLL_EVERY_S = 3.0
MAX_WAIT_S = 3600.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION)
    parser.add_argument("--force", action="store_true",
                        help="Re-render everything, ignoring checksums/timestamps")
    parser.add_argument("--no-rebuild", dest="rebuild", action="store_false",
                        help="Write the files but skip the KB index rebuild")
    parser.add_argument("--status", action="store_true",
                        help="Show the last sync's state and exit")
    half = parser.add_mutually_exclusive_group()
    half.add_argument("--notes-only", action="store_true", help="Sync only notion/notes/")
    half.add_argument("--kanban-only", action="store_true", help="Sync only notion/kanban/")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bidirectional", dest="bidirectional", action="store_true",
                       default=None,
                       help="For notes: also archive Notion pages whose local note was "
                            "deleted (overrides the app's saved setting). Never applies "
                            "to the Kanban mirror, which is derived, not a source.")
    group.add_argument("--no-bidirectional", dest="bidirectional", action="store_false",
                       help="Pull-only (overrides the app's saved setting)")
    return parser


def run(args: list[str] | None = None) -> int:
    parsed = _build_parser().parse_args(args)

    try:
        from src.cli import local_client
    except ImportError as exc:
        print(f"{PROG}: cannot reach the workspace API client: {exc}", file=sys.stderr)
        return 1

    if parsed.status:
        return _print_status(local_client)

    body: dict = {"force": parsed.force, "rebuild": parsed.rebuild,
                  "notes": not parsed.kanban_only, "kanban": not parsed.notes_only}
    if parsed.bidirectional is not None:
        body["bidirectional"] = parsed.bidirectional

    direction = "↔" if parsed.bidirectional else "→"
    print(f"Syncing Notion {direction} the knowledge base"
          f"{'  (force)' if parsed.force else ''}...")

    status, started = local_client.request("POST", "/api/apps/notion/sync", body)
    if status == 409:
        print(f"{PROG}: a sync is already running — poll with --status",
              file=sys.stderr)
        return 1
    if status != 202:
        print(f"{PROG}: could not start the sync ({status}): "
              f"{started.get('error') if isinstance(started, dict) else started}",
              file=sys.stderr)
        return 1

    job = _wait_for_job(local_client)
    if job is None:
        print(f"{PROG}: gave up waiting after {int(MAX_WAIT_S)}s — the job may still "
              f"be running; check with --status", file=sys.stderr)
        return 1
    if job.get("status") == "failed":
        print(f"{PROG}: sync failed: {job.get('error')}", file=sys.stderr)
        return 1

    result = job.get("result") or {}
    for line in result.get("log", []):
        print(f"  {line}")

    notes, kanban = result.get("notes"), result.get("kanban")
    if notes:
        parts = [f"{notes['added']} added", f"{notes['updated']} updated",
                 f"{notes['skipped']} skipped"]
        if notes.get("deleted"):
            parts.append(f"{notes['deleted']} deleted/archived")
        print(f"notes:  {', '.join(parts)}  → {notes.get('notes_dir', '')}")
    if kanban:
        parts = [f"{kanban['added']} added", f"{kanban['updated']} updated",
                 f"{kanban['skipped']} skipped"]
        if kanban.get("moved"):
            parts.append(f"{kanban['moved']} moved status")
        if kanban.get("removed"):
            parts.append(f"{kanban['removed']} removed")
        print(f"kanban: {', '.join(parts)}  → {kanban.get('kanban_dir', '')}")
        by_status = kanban.get("by_status") or {}
        if by_status:
            print("        " + "  ".join(f"{k}={v}" for k, v in by_status.items()))
    if not notes and not kanban:
        print("Nothing synced — neither half is configured.")
        return 1

    # A failed rebuild is not a failed sync — the files are on disk and the
    # next build picks them up — but it must not read as a clean run either.
    rebuild = result.get("kb_rebuild")
    if rebuild and not rebuild.get("ok"):
        print(f"{PROG}: files written, but the KB reindex failed — run "
              f"`aw-workspace-cli knowledge-base --build`", file=sys.stderr)
        return 2
    return 0


def _wait_for_job(local_client) -> dict | None:
    """Poll until the job leaves `running`. Each poll is a short request, so
    a tunnel that kills long-held connections never sees one."""
    deadline = time.monotonic() + MAX_WAIT_S
    while time.monotonic() < deadline:
        status, job = local_client.request("GET", "/api/apps/notion/sync/job")
        if status == 200 and isinstance(job, dict) and job.get("status") != "running":
            return job
        time.sleep(POLL_EVERY_S)
    return None


def _print_status(local_client) -> int:
    status, body = local_client.request("GET", "/api/apps/notion/sync/state")
    if status != 200 or not isinstance(body, dict):
        print(f"{PROG}: could not read sync state ({status}): {body}", file=sys.stderr)
        return 1

    print(f"last sync:     {body.get('last_sync') or 'never'}")
    print()
    print(f"notes:         {'configured' if body.get('notes_configured') else 'NOT configured'}"
          f"  ({body.get('tracked_pages', 0)} tracked)")
    print(f"  root page:   {body.get('root_page_id') or '(unset)'}")
    print(f"  bidirectional: {body.get('bidirectional')}")
    print(f"  dir:         {body.get('notes_dir')}")
    print()
    print(f"kanban:        {'configured' if body.get('kanban_configured') else 'NOT configured'}"
          f"  ({body.get('tracked_cards', 0)} cards)")
    print(f"  comments:    {body.get('kanban_comments')}")
    print(f"  dir:         {body.get('kanban_dir')}")
    for key, count in (body.get("cards_by_status") or {}).items():
        print(f"    {key:<24} {count}")
    return 0
