"""``aw-workspace-cli notion-sync`` — this app's own CLI command.

Replaces the monolith's ``./aw notion-sync``. Auto-discovered by
aw-workspace-cli from this app's installed directory
(``<apps_root>/notion/commands/``, since this file lives at ``commands/`` in
this repo's root — see aw-workspace's ``src/cli/discovery.py``, which loads
every ``<apps_root>/<slug>/commands/*.py`` exposing
``COMMAND``/``DESCRIPTION``/``run``).

Unlike aw-app-remote-host-cli's command, this one does **not** import the
app's package and run the work locally. It can't: the Notion token lives in
this app's zero-knowledge secret store, readable only by the app's own
``ctx.secrets`` facade inside the running workspace process. So the CLI is a
thin client over ``POST /api/apps/notion/sync``, authenticating with the
workspace API key the same way ``aw-workspace-cli marketplace`` does. The
sync engine itself (``notion_app/sync.py``) stays the single source of truth
and is equally reachable from an agent, the UI, or curl.

Usage:
    aw-workspace-cli notion-sync                      # sync changed pages only
    aw-workspace-cli notion-sync --force              # re-sync every page
    aw-workspace-cli notion-sync --bidirectional      # override the saved setting
    aw-workspace-cli notion-sync --no-bidirectional   # pull-only
    aw-workspace-cli notion-sync --no-rebuild         # skip the KB reindex
    aw-workspace-cli notion-sync --status             # what the last sync did
"""
from __future__ import annotations

import argparse
import sys

COMMAND = "notion-sync"
DESCRIPTION = "Sync Notion notes → the knowledge base and rebuild its index"

PROG = "aw-workspace-cli notion-sync"

# A first full sync walks every child page and every nested block, then waits
# on the KB reindex. The client default (30s) times out long before that.
SYNC_TIMEOUT = 1800.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION)
    parser.add_argument("--force", action="store_true",
                        help="Re-sync all pages, ignoring checksums")
    parser.add_argument("--no-rebuild", dest="rebuild", action="store_false",
                        help="Write the notes but skip the KB index rebuild")
    parser.add_argument("--status", action="store_true",
                        help="Show the last sync's state and exit")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bidirectional", dest="bidirectional", action="store_true",
                       default=None,
                       help="Also archive Notion pages whose local note was deleted "
                            "(overrides the app's saved setting)")
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

    body: dict = {"force": parsed.force, "rebuild": parsed.rebuild}
    if parsed.bidirectional is not None:
        body["bidirectional"] = parsed.bidirectional

    direction = "↔" if parsed.bidirectional else "→"
    print(f"Syncing Notion {direction} the knowledge base"
          f"{'  (force)' if parsed.force else ''}...")

    status, result = local_client.request(
        "POST", "/api/apps/notion/sync", body, timeout=SYNC_TIMEOUT)
    if status != 200 or not isinstance(result, dict) or not result.get("ok"):
        print(f"{PROG}: sync failed ({status}): "
              f"{result.get('error') if isinstance(result, dict) else result}",
              file=sys.stderr)
        return 1

    for line in result.get("log", []):
        print(f"  {line}")

    parts = [f"{result['added']} added", f"{result['updated']} updated",
             f"{result['skipped']} skipped"]
    if result.get("deleted"):
        parts.append(f"{result['deleted']} deleted/archived")
    print(f"Done: {', '.join(parts)}.")
    print(f"Notes: {result.get('notes_dir', '')}")

    # A failed rebuild is not a failed sync — the notes are on disk and the
    # next build picks them up — but it must not read as a clean run either.
    rebuild = result.get("kb_rebuild")
    if rebuild and not rebuild.get("ok"):
        print(f"{PROG}: notes written, but the KB reindex failed — run "
              f"`aw-workspace-cli knowledge-base --build`", file=sys.stderr)
        return 2
    return 0


def _print_status(local_client) -> int:
    status, body = local_client.request("GET", "/api/apps/notion/sync/state")
    if status != 200 or not isinstance(body, dict):
        print(f"{PROG}: could not read sync state ({status}): {body}", file=sys.stderr)
        return 1
    if not body.get("configured"):
        missing = "sync_root_page_id" if not body.get("root_page_id") else "a Notion token"
        print(f"not configured — missing {missing}")
    print(f"last sync:     {body.get('last_sync') or 'never'}")
    print(f"tracked pages: {body.get('tracked_pages', 0)}")
    print(f"root page:     {body.get('root_page_id') or '(unset)'}")
    print(f"bidirectional: {body.get('bidirectional')}")
    print(f"notes dir:     {body.get('notes_dir')}")
    return 0
