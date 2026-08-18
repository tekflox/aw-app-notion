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
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

log = logging.getLogger("aw_apps.notion.kanban")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notion rejects any single rich_text object longer than this.
RICH_TEXT_LIMIT = 2000

# Notion accepts a single-part file upload up to 20 MB; past that its API
# wants a multi-part flow with its own part-numbering handshake.
MAX_SINGLE_PART_UPLOAD_BYTES = 20 * 1024 * 1024

# Which Notion block type a filename becomes. Anything unrecognised is a
# generic `file` block, which Notion renders as a download chip.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


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


# Notion's public API allows roughly 3 requests/second, averaged. A single
# tool call never gets near that, but a full board export is ~1000 calls back
# to back and trips it within seconds — the first --force run over 469 cards
# lost two of them to 429s. Both halves of the defence matter: pacing keeps a
# long run under the limit, and retry recovers when a burst crosses it anyway.
MIN_REQUEST_INTERVAL_S = 0.34
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF_S = 2.0


class NotionClient:
    def __init__(self, token_provider: Callable[[], str | None], *, timeout: int = 20) -> None:
        self._token_provider = token_provider
        self._timeout = timeout
        self._pace_lock = threading.Lock()
        self._last_request_at = 0.0

    def _pace(self) -> None:
        """Space requests out to stay under Notion's rate limit.

        Locked because the workspace serves these from a threadpool: two
        concurrent callers (an agent's MCP call landing mid-sync) would
        otherwise each see a stale timestamp and fire together.
        """
        with self._pace_lock:
            wait = MIN_REQUEST_INTERVAL_S - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

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

        for attempt in range(RATE_LIMIT_RETRIES + 1):
            self._pace()
            req = urllib.request.Request(
                f"{NOTION_API}{path}", data=data, headers=self._headers(), method=method)
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < RATE_LIMIT_RETRIES:
                    # Notion tells us how long to wait; its own header beats
                    # any guess we could make. Exponential fallback for the
                    # (common) case where it doesn't send one.
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    try:
                        delay = float(retry_after) if retry_after else RATE_LIMIT_BACKOFF_S * (2 ** attempt)
                    except ValueError:
                        delay = RATE_LIMIT_BACKOFF_S * (2 ** attempt)
                    log.info("notion: rate limited on %s, retrying in %.1fs "
                             "(attempt %d/%d)", path, delay, attempt + 1, RATE_LIMIT_RETRIES)
                    time.sleep(delay)
                    continue
                raise NotionError(e.code, e.read().decode("utf-8", "replace")) from None
            except urllib.error.URLError as e:
                raise NotionError(0, f"could not reach api.notion.com: {e.reason}") from None
        raise NotionError(429, f"still rate limited after {RATE_LIMIT_RETRIES} retries")

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

    # ── file upload ─────────────────────────────────────────────────────
    def upload_file(self, filename: str, content: bytes,
                    content_type: str | None = None) -> str:
        """Push ``content`` through Notion's File Upload API, returning the
        ``file_upload.id`` a block can then reference.

        Two hops, per Notion's API: create an upload slot, then POST the
        bytes to the ``upload_url`` it hands back. The second hop is
        ``multipart/form-data`` and goes to an absolute URL, so it cannot go
        through :meth:`request` (which is JSON-only and path-relative) — but
        it still needs the same pacing, so it calls :meth:`_pace` itself.

        The monolith used ``requests``' ``files=`` to build the multipart
        body. This app is stdlib-only on purpose (see the module docstring),
        so the body is assembled by hand below.
        """
        if not self.configured:
            raise NotionError(401, "no Notion token saved — POST /api/apps/notion/settings first")
        if len(content) > MAX_SINGLE_PART_UPLOAD_BYTES:
            raise NotionError(413, (
                f"{filename} is {len(content) / 1e6:.1f} MB — Notion's single-part upload "
                f"tops out at {MAX_SINGLE_PART_UPLOAD_BYTES / 1e6:.0f} MB. Multi-part upload "
                "is not implemented here; attach a smaller file or a link to it."))

        content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        slot = self.request("POST", "/file_uploads",
                            {"filename": filename, "content_type": content_type})
        upload_id, upload_url = slot.get("id"), slot.get("upload_url")
        if not upload_id or not upload_url:
            raise NotionError(502, f"Notion returned no upload slot: {json.dumps(slot)[:300]}")

        boundary = f"----awNotion{secrets.token_hex(16)}"
        body = _multipart_body(boundary, "file", filename, content_type, content)
        # Content-Type here MUST be the multipart type, not the client's
        # default application/json — hence the hand-built header dict.
        token = self._token_provider() or ""
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        self._pace()
        req = urllib.request.Request(upload_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=max(self._timeout, 60)) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise NotionError(e.code, e.read().decode("utf-8", "replace")) from None
        except urllib.error.URLError as e:
            raise NotionError(0, f"could not reach Notion's upload host: {e.reason}") from None
        return upload_id


def _multipart_body(boundary: str, field: str, filename: str,
                    content_type: str, content: bytes) -> bytes:
    """A single-file ``multipart/form-data`` body.

    ``filename`` goes into a header, so a quote or newline in it would let a
    caller inject extra parts. Callers pass a basename taken from a path the
    user chose, so this sanitises rather than trusts.
    """
    safe = filename.replace('"', "'").replace("\r", " ").replace("\n", " ")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{safe}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode()


def notion_media_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    return "file"


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
