"""Notion → knowledge-base sync, ported from agentic-workspace's
``src/libs/notion_sync.py`` (driven there by ``./aw notion-sync``).

Pull (Notion → local)
    Every child page under ``sync_root_page_id`` is fetched, its blocks are
    converted to Markdown, and written to ``<KB tree>/notes/<slug>.md`` with
    the same frontmatter the monolith wrote (``source``/``repo``/``path``/
    ``notion_id``/``checksum``/``last_edited``), so an already-indexed note
    keeps its identity across the move.

Push (local deletion → Notion archive)
    Only when ``sync_bidirectional`` is on. A slug that was synced before and
    whose local ``.md`` is now gone gets archived (soft-deleted) in Notion; a
    page that vanished from Notion gets its local file removed.

Three things had to change in the move:

* **Where the notes land.** The monolith wrote into its own repo at
  ``docs/knowledge_base/notes/``. Here the knowledge base is a separate
  container app whose indexed tree is the workspace's
  ``<AW_WORKSPACE_HOME>/knowledge_base`` (mounted into aw-app-kb as
  ``$AW_KB_DIR``), so that is the target — writing into this app's own
  package dir would produce notes nothing ever indexes.

* **How the index gets rebuilt.** ``from src.commands.knowledge_base import
  _build`` was an in-process import of a sibling module in the same monolith.
  The KB now runs in its own container, so the rebuild is an HTTP call to it
  and is explicitly *best-effort*: notes that were written stay written even
  if the rebuild fails, and the failure is reported rather than swallowed.

* **stdlib instead of ``requests``.** Same reason as ``kanban/client.py`` —
  aw-workspace core does not install an app's ``runtime.pip_requires``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .kanban.client import NotionClient, NotionError

log = logging.getLogger("aw_apps.notion.sync")

ROOT_PAGE_KEY = "sync_root_page_id"
BIDIRECTIONAL_KEY = "sync_bidirectional"
NOTES_SUBDIR = "notes"
STATE_FILENAME = "notion_sync_state.json"

# The KB app's own build endpoint. Reachable from the workspace process by
# container name on the shared app network; overridable for a workspace that
# renames or relocates it.
KB_BUILD_URL = os.environ.get("AW_KB_BUILD_URL", "http://aw-app-kb:8000/api/kb/build")


def workspace_home() -> str:
    """Same resolution as core's ``src/apps/paths.py::workspace_home_path``,
    reimplemented rather than imported: an app must not depend on core's
    private module layout."""
    home = os.environ.get("AW_WORKSPACE_HOME")
    if home:
        return home
    root = os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace"))
    return os.path.join(root, ".aw-workspace")


def notes_dir() -> str:
    return os.path.join(workspace_home(), "knowledge_base", NOTES_SUBDIR)


def state_path() -> str:
    return os.path.join(workspace_home(), "data", "notion", STATE_FILENAME)


# ── markdown conversion (ported verbatim in behaviour) ──────────────────

def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _slugify(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug or "untitled"


def _rich_text_to_md(rich_text: list) -> str:
    result = ""
    for t in rich_text:
        text = t.get("plain_text", "")
        ann = t.get("annotations", {})
        if ann.get("code"):
            text = f"`{text}`"
        if ann.get("bold"):
            text = f"**{text}**"
        if ann.get("italic"):
            text = f"*{text}*"
        if ann.get("strikethrough"):
            text = f"~~{text}~~"
        result += text
    return result


def _get_block_children(client: NotionClient, block_id: str) -> list:
    blocks: list = []
    cursor = None
    while True:
        query = {"page_size": 100}
        if cursor:
            query["start_cursor"] = cursor
        path = f"/blocks/{block_id}/children?{urllib.parse.urlencode(query)}"
        data = client.request("GET", path)
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return blocks


def _blocks_to_md(client: NotionClient, blocks: list, depth: int = 0) -> str:
    lines: list[str] = []
    indent = "  " * depth

    for block in blocks:
        btype = block.get("type", "")
        data = block.get(btype, {})

        if btype == "paragraph":
            text = _rich_text_to_md(data.get("rich_text", []))
            lines.append(f"{indent}{text}" if text else "")
        elif btype in ("heading_1", "heading_2", "heading_3"):
            level = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}[btype]
            lines.append(f"{level} {_rich_text_to_md(data.get('rich_text', []))}")
        elif btype == "bulleted_list_item":
            lines.append(f"{indent}- {_rich_text_to_md(data.get('rich_text', []))}")
        elif btype == "numbered_list_item":
            lines.append(f"{indent}1. {_rich_text_to_md(data.get('rich_text', []))}")
        elif btype == "to_do":
            checked = "x" if data.get("checked") else " "
            lines.append(f"{indent}- [{checked}] {_rich_text_to_md(data.get('rich_text', []))}")
        elif btype == "code":
            lang = data.get("language", "")
            lines.append(f"```{lang}\n{_rich_text_to_md(data.get('rich_text', []))}\n```")
        elif btype == "quote":
            lines.append(f"> {_rich_text_to_md(data.get('rich_text', []))}")
        elif btype == "callout":
            emoji = data.get("icon", {}).get("emoji", "")
            lines.append(f"> {emoji} {_rich_text_to_md(data.get('rich_text', []))}")
        elif btype == "divider":
            lines.append("---")
        elif btype == "image":
            url = (data.get("external", {}).get("url")
                   or data.get("file", {}).get("url", ""))
            lines.append(f"![{_rich_text_to_md(data.get('caption', []))}]({url})")
        elif btype == "bookmark":
            url = data.get("url", "")
            lines.append(f"[{url}]({url})")
        elif btype == "toggle":
            lines.append(f"**{_rich_text_to_md(data.get('rich_text', []))}**")
        elif btype == "child_page":
            child_md = _blocks_to_md(client, _get_block_children(client, block["id"]), depth)
            lines.append(f"## {data.get('title', '')}")
            if child_md:
                lines.append(child_md)

        if block.get("has_children") and btype not in ("child_page", "code"):
            nested = _blocks_to_md(client, _get_block_children(client, block["id"]), depth + 1)
            if nested:
                lines.append(nested)

        lines.append("")

    return "\n".join(lines).strip()


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return "Untitled"


def _render_page(client: NotionClient, page_id: str) -> tuple[str, str, str] | None:
    """Fetch a page and render it. Returns (slug, last_edited, content) or
    None if the page couldn't be fetched — one unreadable page must not abort
    a sync of fifty others."""
    try:
        page = client.request("GET", f"/pages/{page_id}")
    except NotionError as exc:
        log.warning("notion-sync: could not fetch page %s: %s", page_id, exc)
        return None

    title = _page_title(page)
    last_edited = page.get("last_edited_time", "")
    slug = _slugify(title)

    body = _blocks_to_md(client, _get_block_children(client, page_id))
    if not body.strip():
        body = "*(página vazia)*"

    full_content = f"# {title}\n\n{body}"
    content = "\n".join([
        "---",
        "source: notion",
        "repo: notes",
        f"path: {NOTES_SUBDIR}/{slug}.md",
        f"notion_id: {page_id}",
        f"checksum: {_sha256(full_content)}",
        f"last_edited: {last_edited}",
        "---",
        "",
        full_content,
        "",
    ])
    return slug, last_edited, content


# ── state ───────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(state_path()) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _rebuild_kb() -> dict:
    """Ask the KB app to reindex. Best-effort by design — the notes are
    already on disk and a failed rebuild only means they're indexed on the
    next build, so this reports rather than raises."""
    req = urllib.request.Request(KB_BUILD_URL, data=b"{}",
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return {"ok": 200 <= resp.status < 300, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:  # noqa: BLE001 — connection refused, DNS, timeout
        return {"ok": False, "status": 0, "error": str(e)}


# ── the sync ────────────────────────────────────────────────────────────

def run_sync(client: NotionClient, root_page_id: str, *, force: bool = False,
             bidirectional: bool = False, rebuild: bool = True) -> dict:
    """Sync Notion pages under ``root_page_id`` into the KB's ``notes/`` tree.

    Returns ``{added, updated, skipped, deleted, notes_dir, log, kb_rebuild}``.
    ``log`` carries the per-page lines the monolith printed to stdout — this
    runs inside the workspace server, where nobody would ever see a print, so
    the CLI renders them instead.
    """
    if not root_page_id:
        raise NotionError(503, f"{ROOT_PAGE_KEY} is not configured — "
                               f"POST /api/apps/notion/config {{\"{ROOT_PAGE_KEY}\": \"...\"}}")

    target = notes_dir()
    os.makedirs(target, exist_ok=True)
    state = _load_state()
    tracked: dict = state.get("pages", {})
    lines: list[str] = []
    deleted = 0

    # Push: archive Notion pages whose local file was deleted.
    if bidirectional:
        for page_id, info in list(tracked.items()):
            slug = info.get("slug", "")
            if os.path.exists(os.path.join(target, f"{slug}.md")):
                continue
            try:
                client.request("PATCH", f"/pages/{page_id}", {"archived": True})
            except NotionError as exc:
                lines.append(f"warn: failed to archive {page_id} in Notion — {exc}")
                continue
            lines.append(f"archived in notion: {slug} ({page_id})")
            del tracked[page_id]
            deleted += 1

    child_pages = [b for b in _get_block_children(client, root_page_id)
                   if b.get("type") == "child_page"]
    active_ids = {b["id"] for b in child_pages}

    # Push: drop local files for pages that disappeared from Notion.
    if bidirectional:
        for page_id, info in list(tracked.items()):
            if page_id in active_ids:
                continue
            slug = info.get("slug", "")
            local = os.path.join(target, f"{slug}.md")
            if os.path.exists(local):
                os.remove(local)
                lines.append(f"removed local (deleted in Notion): {slug}.md")
                deleted += 1
            del tracked[page_id]

    added = updated = skipped = 0
    for block in child_pages:
        page_id = block["id"]
        rendered = _render_page(client, page_id)
        if rendered is None:
            lines.append(f"warn: failed to fetch page {page_id}")
            continue

        slug, last_edited, content = rendered
        out_path = os.path.join(target, f"{slug}.md")

        if os.path.exists(out_path) and not force:
            with open(out_path) as f:
                if f.read() == content:
                    tracked[page_id] = {"slug": slug, "last_edited": last_edited}
                    skipped += 1
                    continue
            updated += 1
            action = "updated"
        else:
            added += 1
            action = "added"

        with open(out_path, "w") as f:
            f.write(content)
        tracked[page_id] = {"slug": slug, "last_edited": last_edited}
        lines.append(f"{action}: {slug}.md  ({last_edited})")

    kb_rebuild = None
    if rebuild and (added or updated or deleted):
        kb_rebuild = _rebuild_kb()
        lines.append("KB index rebuilt" if kb_rebuild["ok"]
                     else f"warn: KB rebuild failed — {kb_rebuild.get('error', kb_rebuild)}")

    _save_state({"last_sync": datetime.now(timezone.utc).isoformat(), "pages": tracked})
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped,
            "deleted": deleted, "notes_dir": target, "log": lines,
            "kb_rebuild": kb_rebuild}


def sync_state() -> dict:
    state = _load_state()
    return {"last_sync": state.get("last_sync"), "tracked_pages": len(state.get("pages", {})),
            "notes_dir": notes_dir(), "state_file": state_path()}
