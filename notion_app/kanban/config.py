"""Board configuration — ported from the monolith's ``src/config/aw.json``
key ``notion.agents_kanban`` (``database_id`` + a logical-status → Notion
option-name map).

In the monolith this was a hand-edited block in a git-committed config file.
Here it is ordinary app config (``ctx.config``, saved through core's generic
``POST /api/apps/notion/config``), so it round-trips with the rest of the
workspace instead of living in a file only the monolith knew to read.

The status map ships with the monolith's values as defaults, so a fresh
install of this app against the same board works with nothing but a
``kanban_database_id``. Override any subset via ``kanban_statuses`` — the
override is merged over the defaults, not swapped for them, so renaming one
Notion option doesn't require restating the other ten.
"""
from __future__ import annotations

# Verbatim from agentic-workspace src/config/aw.json → notion.agents_kanban.
# "auto_resolved" displays as "Self-closed" (renamed 2026-07-14) — the logical
# key is kept so existing agent prompts and skills don't have to change.
DEFAULT_STATUSES: dict[str, str] = {
    "archived": "Archived",
    "auto_resolved": "Self-closed",
    "backlog": "Backlog",
    "done": "Done",
    "done_archived": "Done Archived",
    "need_human": "Need Human",
    "planned": "Planned",
    "ready": "Ready",
    "ready_to_deploy": "Ready to Deploy",
    "running": "In Progress",
    "self_closed_archived": "Self-closed Archived",
}

DB_ID_KEY = "kanban_database_id"
STATUSES_KEY = "kanban_statuses"


class KanbanConfig:
    """Reads ``ctx.config`` lazily on every access.

    Core rebinds ``ctx.config`` to a fresh dict on each config save (see
    aw-workspace ``src/apps/routes.py::save_app_config``), so holding the
    ``ctx`` and re-reading beats snapshotting the dict at activate() time —
    a database_id saved after boot takes effect without a restart.
    """

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    @property
    def database_id(self) -> str:
        return str((getattr(self._ctx, "config", None) or {}).get(DB_ID_KEY) or "").strip()

    @property
    def statuses(self) -> dict[str, str]:
        override = (getattr(self._ctx, "config", None) or {}).get(STATUSES_KEY) or {}
        merged = dict(DEFAULT_STATUSES)
        if isinstance(override, dict):
            merged.update({str(k): str(v) for k, v in override.items()})
        return merged

    def notion_status(self, logical: str) -> str:
        """Logical key ('need_human') → Notion option name ('Need Human').

        An unknown key is passed through unchanged rather than rejected: the
        monolith's ``move_kanban_task`` accepted a raw Notion option name too,
        and agents in the wild pass both.
        """
        key = (logical or "").strip()
        return self.statuses.get(key, key)

    @property
    def configured(self) -> bool:
        return bool(self.database_id)
