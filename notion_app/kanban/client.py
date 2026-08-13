"""Thin Notion REST client + the property/rich-text plumbing ported from
the monolith's ``src/api/kanban_manager.py``.

Two deliberate departures from the original:

* **stdlib ``urllib`` instead of ``requests``.** aw-workspace core does not
  install an app's ``runtime.pip_requires`` (it is parsed and ignored), so a
  third-party import here would be an ``ImportError`` at activate() time on a
  clean workspace, taking the whole app's routes down with it. Everything
  below is stdlib.
* **the token is passed in, not read off disk.** The monolith reopened
  ``aw.json`` on every single call to rebuild its auth header. Here the token
  lives in this app's secret store and is resolved per call through a
  callable, so a token saved (or cleared) at runtime is picked up without a
  restart and never gets copied into a module global.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Callable

log = logging.getLogger("aw_apps.notion.kanban")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notion rejects any single rich_text object longer than this.
RICH_TEXT_LIMIT = 2000


class NotionError(RuntimeError):
    """A Notion API call that came back non-2xx, with the body preserved.

    Notion's error bodies are the useful part (``object_not_found`` almost
    always means "the integration was never shared with this page", which is
    the single most common failure here) — swallowing them and surfacing a
    bare status code sends people debugging the wrong layer.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Notion API {status}: {body[:400]}")
        self.status = status
        self.body = body


class NotionClient:
    def __init__(self, token_provider: Callable[[], str | None], *, timeout: int = 20) -> None:
        self._token_provider = token_provider
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._token_provider())

    def _headers(self) -> dict[str, str]:
        token = self._token_provider() or ""
        return {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.configured:
            raise NotionError(401, "no Notion token saved — POST /api/apps/notion/settings first")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{NOTION_API}{path}", data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise NotionError(e.code, e.read().decode("utf-8", "replace")) from None
        except urllib.error.URLError as e:
            raise NotionError(0, f"could not reach api.notion.com: {e.reason}") from None

    # ── convenience wrappers ────────────────────────────────────────────
    def get_page(self, page_id: str) -> dict:
        return self.request("GET", f"/pages/{page_id}")

    def patch_page(self, page_id: str, properties: dict) -> dict:
        return self.request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def create_page(self, body: dict) -> dict:
        return self.request("POST", "/pages", body)

    def query_database(self, database_id: str, body: dict) -> dict:
        return self.request("POST", f"/databases/{database_id}/query", body)

    def database_schema(self, database_id: str) -> dict:
        """Live property schema (name → spec incl. ``type``).

        Fetched fresh per call, like the monolith did: the board's schema
        changes rarely, this is not a hot path, and a cache here would need
        invalidation nobody would remember to wire up.
        """
        return self.request("GET", f"/databases/{database_id}").get("properties", {})

    def append_blocks(self, page_id: str, children: list[dict]) -> dict:
        return self.request("PATCH", f"/blocks/{page_id}/children", {"children": children})

    def post_comment(self, page_id: str, rich_text: list[dict]) -> dict:
        return self.request("POST", "/comments",
                            {"parent": {"page_id": page_id}, "rich_text": rich_text})


# ── text / property helpers (ported verbatim in behaviour) ──────────────

def split_text_blocks(text: str, max_len: int = RICH_TEXT_LIMIT) -> list[str]:
    """Split long text into chunks ≤ max_len, preferring paragraph breaks."""
    chunks: list[str] = []
    remaining = (text or "").strip()
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        cut = remaining[:max_len]
        for sep in ("\n\n", "\n"):
            idx = cut.rfind(sep)
            if idx > max_len // 2:
                chunks.append(cut[:idx].rstrip())
                remaining = remaining[idx:].lstrip()
                break
        else:
            chunks.append(cut)
            remaining = remaining[max_len:]
    return [c for c in chunks if c]


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def text_to_rich_text(text: str, max_len: int = RICH_TEXT_LIMIT) -> list[dict]:
    """Convert text into Notion rich_text objects, turning `[label](url)`
    markdown links into real clickable Notion links rather than literal
    brackets."""
    rich_text: list[dict] = []
    last = 0
    for m in _MARKDOWN_LINK_RE.finditer(text):
        plain = text[last:m.start()]
        if plain:
            chunks = [plain] if len(plain) <= max_len else split_text_blocks(plain, max_len)
            rich_text.extend({"type": "text", "text": {"content": c}} for c in chunks)
        rich_text.append({"type": "text",
                          "text": {"content": m.group(1), "link": {"url": m.group(2)}}})
        last = m.end()
    remainder = text[last:]
    if remainder:
        chunks = [remainder] if len(remainder) <= max_len else split_text_blocks(remainder, max_len)
        rich_text.extend({"type": "text", "text": {"content": c}} for c in chunks)
    return rich_text


def paragraph_blocks(text: str) -> list[dict]:
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}}
            for chunk in split_text_blocks(text)]


def heading_block(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def build_property_payload(prop_type: str, value: Any) -> dict:
    """Notion value wrapper for one property, given its schema ``type`` and a
    plain Python value. Raises ValueError for a type we can't set."""
    if value is None:
        # Clearing a property: each type's "empty" shape differs.
        return {
            "checkbox": {"checkbox": False},
            "select": {"select": None},
            "multi_select": {"multi_select": []},
            "rich_text": {"rich_text": []},
            "number": {"number": None},
            "date": {"date": None},
            "url": {"url": None},
        }.get(prop_type, {prop_type: None})
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    if prop_type == "select":
        return {"select": {"name": str(value)}}
    if prop_type == "status":
        # Not handled by the monolith — its board used a Select for Status.
        # A board created after Notion shipped the native Status type needs
        # this branch or every move_kanban_task would fail as "unsupported".
        return {"status": {"name": str(value)}}
    if prop_type == "multi_select":
        names = value if isinstance(value, list) else [value]
        return {"multi_select": [{"name": str(n)} for n in names]}
    if prop_type == "rich_text":
        return {"rich_text": [{"text": {"content": str(value)[:RICH_TEXT_LIMIT]}}]}
    if prop_type == "number":
        return {"number": float(value)}
    if prop_type == "date":
        return {"date": {"start": str(value)}}
    if prop_type == "url":
        return {"url": str(value)}
    if prop_type == "title":
        return {"title": [{"text": {"content": str(value)[:RICH_TEXT_LIMIT]}}]}
    raise ValueError(f"unsupported property type '{prop_type}'")


def extract_property_value(prop: dict) -> Any:
    """Inverse of :func:`build_property_payload`."""
    ptype = prop.get("type", "")
    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if ptype == "select":
        return (prop.get("select") or {}).get("name")
    if ptype == "status":
        return (prop.get("status") or {}).get("name")
    if ptype == "multi_select":
        return [o.get("name") for o in prop.get("multi_select", [])]
    if ptype == "checkbox":
        return prop.get("checkbox", False)
    if ptype == "number":
        return prop.get("number")
    if ptype == "date":
        return (prop.get("date") or {}).get("start")
    if ptype == "url":
        return prop.get("url")
    if ptype == "people":
        return [p.get("name") or p.get("id") for p in prop.get("people", [])]
    if ptype in ("created_time", "last_edited_time"):
        return prop.get(ptype)
    return None


def page_title(page: dict) -> str:
    """The card's title, without needing to know which property holds it.

    The monolith wrote ``Name`` but read ``Task`` in places, and a board
    renamed in Notion's UI breaks any hardcoded guess — so find the property
    whose *type* is ``title``, which Notion guarantees is exactly one.
    """
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return ""


def status_property_name(schema: dict) -> str:
    """Name of the board's status property. ``Status`` when present (the
    monolith's board), otherwise the first select/status-typed property."""
    if "Status" in schema:
        return "Status"
    for name, spec in schema.items():
        if spec.get("type") in ("status", "select"):
            return name
    return "Status"
