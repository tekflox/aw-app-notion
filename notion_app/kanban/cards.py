"""Board operations — the portable half of the monolith's
``src/api/routes/notion_kanban.py`` + ``src/api/kanban_manager.py``.

Everything here is expressible as "a Notion database used as a Kanban
board". What is *not* here, and why:

* the ``POST /api/notion/webhook`` dispatcher, ``run_ready_cards``,
  ``invoke_kanban_agent``, and ``start_now`` — these fire and resume
  agents-platform runs. They belong to agents-platform, not to Notion, and
  reimplementing them here would hardcode this app to one orchestrator.
* the Telegram approval keyboard (``send_approval_batch``) — owned by the
  Agents Platform bot, reachable only from the monolith's network position.
* ``attach_kanban_presentation`` — cross-app coupling to aw-app-presentations.

``create_card`` therefore creates the card and stops. The monolith's
version created *and* notified *and* optionally dispatched, in one call; the
notification half returns ``approval_sent: false`` here with a note, rather
than silently doing nothing under a name that promises delivery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .client import (
    NotionClient,
    NotionError,
    build_property_payload,
    extract_property_value,
    heading_block,
    page_title,
    paragraph_blocks,
    split_text_blocks,
    status_property_name,
    text_to_rich_text,
)
from .config import KanbanConfig

log = logging.getLogger("aw_apps.notion.kanban")

# Priority labels the monolith accepted (pt-BR) → the board's option names.
_PRIORITY_MAP = {
    "Alta": "High", "Média": "Medium", "Media": "Medium", "Baixa": "Low",
    "High": "High", "Medium": "Medium", "Low": "Low",
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class KanbanBoard:
    def __init__(self, client: NotionClient, config: KanbanConfig) -> None:
        self.client = client
        self.config = config

    def _require_db(self) -> str:
        db_id = self.config.database_id
        if not db_id:
            raise NotionError(503, "kanban_database_id is not configured — "
                                   "POST /api/apps/notion/config {\"kanban_database_id\": \"...\"}")
        return db_id

    # ── read ────────────────────────────────────────────────────────────
    def list_cards(self, *, status: str = "", source: str = "", limit: int = 25,
                   order: str = "created") -> dict:
        """Query the board. ``status`` is a logical key or a raw Notion option
        name; omit it for every card regardless of status.

        Sorted newest-first by default. ``order='edited'`` sorts by last edit
        instead — the monolith had no list tool at all (``_list_kanban_cards``
        existed in src/mcp/kanban.py but was never registered in its TOOLS
        list, so no agent could ever call it), so this is new surface rather
        than a port.
        """
        db_id = self._require_db()
        timestamp = "last_edited_time" if order == "edited" else "created_time"
        body: dict[str, Any] = {
            "sorts": [{"timestamp": timestamp, "direction": "descending"}],
            "page_size": max(1, min(int(limit or 25), 100)),
        }

        filters: list[dict] = []
        if status:
            schema = self.client.database_schema(db_id)
            prop_name = status_property_name(schema)
            prop_type = schema.get(prop_name, {}).get("type", "select")
            filters.append({"property": prop_name,
                            prop_type: {"equals": self.config.notion_status(status)}})
        if source:
            filters.append({"property": "Source", "rich_text": {"equals": source}})
        if len(filters) == 1:
            body["filter"] = filters[0]
        elif filters:
            body["filter"] = {"and": filters}

        result = self.client.query_database(db_id, body)
        return {
            "count": len(result.get("results", [])),
            "has_more": result.get("has_more", False),
            "cards": [self._summarise(p) for p in result.get("results", [])],
        }

    def _summarise(self, page: dict) -> dict:
        props = page.get("properties", {})

        def val(name: str):
            return extract_property_value(props[name]) if name in props else None

        schema_status = val("Status")
        return {
            "page_id": page.get("id", ""),
            "title": page_title(page),
            "status": schema_status,
            "priority": val("Priority"),
            "source": val("Source"),
            "agent_slug": val("AgentSlug"),
            "target_slug": val("TargetSlug"),
            "finding_key": val("FindingKey"),
            "tags": val("Tags"),
            "occurrence_count": val("OccurrenceCount"),
            "created_time": page.get("created_time"),
            "last_edited_time": page.get("last_edited_time"),
            "url": page.get("url", ""),
        }

    def get_card(self, page_id: str) -> dict:
        return self._summarise(self.client.get_page(page_id))

    def get_properties(self, page_id: str, names: list[str] | None = None) -> dict:
        props = self.client.get_page(page_id).get("properties", {})
        if names:
            props = {k: v for k, v in props.items() if k in names}
        return {k: extract_property_value(v) for k, v in props.items()}

    # ── write ───────────────────────────────────────────────────────────
    def set_property(self, page_id: str, property_name: str, value: Any) -> dict:
        db_id = self._require_db()
        schema = self.client.database_schema(db_id)
        spec = schema.get(property_name)
        if not spec:
            return {"ok": False,
                    "error": f"unknown property '{property_name}' — not in the board schema",
                    "available": sorted(schema.keys())}
        try:
            payload = build_property_payload(spec.get("type", ""), value)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        self.client.patch_page(page_id, {property_name: payload})
        return {"ok": True, "page_id": page_id, "property": property_name, "value": value}

    def add_comment(self, page_id: str, text: str) -> dict:
        """Post a comment, chunked to Notion's rich_text limit.

        The monolith prefixed every comment with an agents-platform run
        byline (``**agent_slug** — [run_id](url)``), resolved by querying
        ``/api/runs?notion_task_id=``. That lookup is agents-platform-only, so
        it is dropped here rather than faked — a caller that wants a byline
        can just put one in ``text``.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "text is required"}
        rich_text = text_to_rich_text(text)
        if not rich_text:
            return {"ok": False, "error": "text produced no content"}
        self.client.post_comment(page_id, rich_text)
        return {"ok": True, "page_id": page_id}

    def move_card(self, page_id: str, status: str, comment: str = "") -> dict:
        """Move a card's status, posting ``comment`` first if given.

        ``need_human`` requires a comment — same guard as the monolith. A
        card parked for a human with no explanation of *what* the human is
        supposed to decide is the failure this rule exists to prevent.
        """
        if not status:
            return {"ok": False, "error": "status is required"}
        logical = status.strip()
        if logical == "need_human" and not (comment or "").strip():
            return {"ok": False,
                    "error": "status='need_human' requires a comment explaining the problem, "
                             "the options you see, and what needs to be decided"}

        db_id = self._require_db()
        schema = self.client.database_schema(db_id)
        prop_name = status_property_name(schema)
        prop_type = schema.get(prop_name, {}).get("type", "select")
        notion_status = self.config.notion_status(logical)

        commented = False
        if (comment or "").strip():
            commented = self.add_comment(page_id, comment).get("ok", False)

        self.client.patch_page(
            page_id, {prop_name: build_property_payload(prop_type, notion_status)})
        return {"ok": True, "page_id": page_id, "status": notion_status,
                "comment_posted": commented,
                "note": "Status updated in Notion. Dispatching an agent run on 'ready' is "
                        "agents-platform's job and is not wired into this app."}

    def set_qa_status(self, page_id: str, status: str, comment: str = "") -> dict:
        """QA verdict: stamp ``QAStatus`` when the board has it, then move.

        Without a ``page_id`` the caller has no card — the monolith returned a
        soft ok so QA agents could call it unconditionally at the end of every
        review. Same here.
        """
        if not status:
            return {"ok": False, "error": "status is required"}
        if not page_id:
            return {"ok": True, "page_id": None, "status": status,
                    "comment": comment or None,
                    "note": "No page_id — verdict recorded for this run only, nothing "
                            "persisted to Notion. Call again with page_id if this work is "
                            "later tied to a Kanban card."}
        stamped = self.set_property(page_id, "QAStatus", status)
        moved = self.move_card(page_id, status, comment)
        return {"ok": moved.get("ok", False), "page_id": page_id, "status": status,
                "qa_status_stamped": stamped.get("ok", False), "move": moved}

    def set_blocker(self, page_id: str, comment: str) -> dict:
        """Blocked mid-task → comment + move to need_human.

        The monolith also pinged Frederico on Telegram from here. That
        notification is the Agents Platform bot's, so the card move is all
        this app can honestly do — said plainly in the response rather than
        left for the caller to assume.
        """
        if not (comment or "").strip():
            return {"ok": False, "error": "comment is required — say what's blocking you, "
                                          "what you tried, and what would unblock it"}
        result = self.move_card(page_id, "need_human", f"🚧 Blocker\n\n{comment.strip()}")
        result["note"] = ("Card moved to Need Human. No Telegram ping was sent — that "
                          "notification belongs to agents-platform, not this app.")
        return result

    # ── create ──────────────────────────────────────────────────────────
    def create_card(self, *, title: str, finding_key: str = "", priority: str = "Média",
                    agent_slug: str = "", target_slug: str = "", input_text: str = "",
                    check_hint: str = "", description: str = "", plan: str = "",
                    source: str = "", tags: list[str] | None = None) -> dict:
        """Create a card, or bump the existing one with the same
        ``finding_key`` (dedup + occurrence counting + regression detection,
        all ported from the monolith's ``create_task_endpoint``).

        Unlike the monolith this never validates ``target_slug`` against
        agents-platform and never dispatches a run — see the module docstring.
        """
        title = (title or "").strip()
        if not title:
            return {"ok": False, "error": "title is required"}
        db_id = self._require_db()
        tags = tags or []
        input_text = input_text or f"Execute task: {title}"
        today = _today()

        existing = self._find_by_finding_key(db_id, finding_key) if finding_key else None
        if existing:
            return self._bump_existing(existing, plan=plan, today=today)

        schema = self.client.database_schema(db_id)
        status_prop = status_property_name(schema)
        status_type = schema.get(status_prop, {}).get("type", "select")
        title_prop = next((n for n, s in schema.items() if s.get("type") == "title"), "Name")

        properties: dict[str, Any] = {
            title_prop: {"title": [{"text": {"content": title}}]},
            status_prop: build_property_payload(
                status_type, self.config.notion_status("backlog")),
        }
        # Only write properties the board actually has — the monolith wrote a
        # fixed set and 400'd against any board shaped even slightly
        # differently.
        optional = {
            "Priority": _PRIORITY_MAP.get(priority, "Medium"),
            "Source": source or "",
            "TargetSlug": target_slug or "",
            "AgentSlug": agent_slug or "",
            "FindingKey": finding_key or "",
            "CheckHint": (check_hint or "")[:500],
            "OccurrenceCount": 1,
            "LastSeenAt": today,
            "Tags": tags[:5] if tags else None,
        }
        for name, value in optional.items():
            if name not in schema or value in ("", None, []):
                continue
            try:
                properties[name] = build_property_payload(schema[name].get("type", ""), value)
            except ValueError:
                log.warning("kanban: skipping property %r — unsupported type %r",
                            name, schema[name].get("type"))

        children: list[dict] = []
        if description:
            children.append({"object": "block", "type": "callout", "callout": {
                "rich_text": [{"type": "text", "text": {"content": description[:2000]}}],
                "icon": {"type": "emoji", "emoji": "🔍"},
                "color": "yellow_background",
            }})
        children.append(heading_block("🎯 Instruções para o executor"))
        children.extend(paragraph_blocks(input_text))
        if plan:
            children.append(heading_block("📋 Análise & Plano de Execução"))
            children.extend(paragraph_blocks(plan))

        page = self.client.create_page(
            {"parent": {"database_id": db_id}, "properties": properties, "children": children})
        return {"ok": True, "page_id": page.get("id", ""), "url": page.get("url", ""),
                "is_new": True, "occurrence_count": 1,
                "status": self.config.notion_status("backlog"),
                "approval_sent": False,
                "note": "Card created in Backlog. No Telegram approval was sent and no run "
                        "was dispatched — both belong to agents-platform, not this app."}

    def _find_by_finding_key(self, db_id: str, finding_key: str) -> dict | None:
        try:
            result = self.client.query_database(db_id, {
                "filter": {"property": "FindingKey", "rich_text": {"equals": finding_key}},
                "page_size": 1,
            })
        except NotionError as exc:
            # A board without a FindingKey property can't dedup; that's a
            # degraded create, not a failure.
            log.warning("kanban: dedup query failed for finding_key=%s: %s", finding_key, exc)
            return None
        results = result.get("results", [])
        return results[0] if results else None

    def _bump_existing(self, page: dict, *, plan: str, today: str) -> dict:
        page_id = page["id"]
        props = page.get("properties", {})
        current_status = extract_property_value(props.get("Status", {})) or ""

        if current_status == self.config.notion_status("archived"):
            return {"ok": True, "page_id": page_id, "is_new": False, "skipped": True,
                    "reason": "archived", "url": page.get("url", "")}

        occ = (props.get("OccurrenceCount", {}).get("number") or 0) + 1
        is_regression = current_status == self.config.notion_status("done")

        patch: dict[str, Any] = {}
        if "OccurrenceCount" in props:
            patch["OccurrenceCount"] = {"number": occ}
        if "LastSeenAt" in props:
            patch["LastSeenAt"] = {"date": {"start": today}}
        if is_regression and "Status" in props:
            patch["Status"] = build_property_payload(
                props["Status"].get("type", "select"), self.config.notion_status("backlog"))
        if patch:
            self.client.patch_page(page_id, patch)

        note = " ⚠ REGRESSÃO" if is_regression else ""
        blocks: list[dict] = [{"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {
                "content": f"🔁 Re-detectado em {today}{note} (ocorrência #{occ})"}}],
            "icon": {"type": "emoji", "emoji": "🔁"},
            "color": "orange_background" if is_regression else "gray_background",
        }}]
        if plan:
            blocks.append(heading_block(f"📋 Plano — {today}"))
            blocks.extend(paragraph_blocks(plan))
        self.client.append_blocks(page_id, blocks)

        return {"ok": True, "page_id": page_id, "url": page.get("url", ""), "is_new": False,
                "occurrence_count": occ, "regression": is_regression,
                "status": self.config.notion_status("backlog") if is_regression else current_status}


__all__ = ["KanbanBoard", "split_text_blocks"]
