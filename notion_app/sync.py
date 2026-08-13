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
COMMENTS_KEY = "sync_kanban_comments"

# Everything this app mirrors lives under one ``notion/`` root inside the KB
# tree, so a KB search result's path says where it came from and a stale
# mirror can be dropped wholesale. ``notes/`` at the top level (v0.5.0) said
# nothing about its origin and collided with anything else wanting that name.
MIRROR_SUBDIR = "notion"
NOTES_SUBDIR = f"{MIRROR_SUBDIR}/notes"
KANBAN_SUBDIR = f"{MIRROR_SUBDIR}/kanban"
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


def kb_dir() -> str:
    return os.path.join(workspace_home(), "knowledge_base")


def notes_dir() -> str:
    return os.path.join(kb_dir(), NOTES_SUBDIR)


def kanban_dir() -> str:
    return os.path.join(kb_dir(), KANBAN_SUBDIR)


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


def _prune_empty_dirs(root: str) -> None:
    """Drop status dirs left empty after cards moved out of them. A stale
    ``kanban/ready/`` with nothing in it reads as "nothing is ready", which is
    a different claim from "that status doesn't exist here"."""
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)


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


# ── kanban export ───────────────────────────────────────────────────────

def _status_dirname(notion_status: str, statuses: dict[str, str]) -> str:
    """Notion option name → the directory it belongs in.

    Prefers the *logical* key from the app's status map ('Need Human' →
    ``need_human``), so the tree is stable when a Notion option is renamed —
    "Auto-resolvido" → "Self-closed" happened once already and moved every
    card. An option with no logical key falls back to its slugified name
    rather than being dropped on the floor.
    """
    reverse = {v: k for k, v in statuses.items()}
    if notion_status in reverse:
        return reverse[notion_status]
    return _slugify(notion_status) if notion_status else "no-status"


def _card_comments_md(client: NotionClient, page_id: str) -> str:
    """The card's comment thread, oldest → newest.

    This is where the real history lives on this board — dev delivery
    reports, QA verdicts, Frederico's notes — so a mirror without it is a
    mirror of the task description only.
    """
    try:
        data = client.request(
            "GET", f"/comments?{urllib.parse.urlencode({'block_id': page_id, 'page_size': 100})}")
    except NotionError as exc:
        log.warning("notion-sync: could not read comments for %s: %s", page_id, exc)
        return ""
    out: list[str] = []
    for c in data.get("results", []):
        text = "".join(t.get("plain_text", "") for t in c.get("rich_text", []))
        if text.strip():
            out.append(f"- **{c.get('created_time', '')}** — {text.strip()}")
    return "\n".join(out)


def _card_status(card: dict) -> str:
    """The card's Notion status option name, straight off the query result."""
    prop = card.get("properties", {}).get("Status", {})
    return ((prop.get("select") or prop.get("status") or {}) or {}).get("name", "")


def _card_slugs(cards: list[dict], statuses: dict[str, str]) -> dict[str, str]:
    """page_id → the slug to use, with collisions disambiguated.

    Card titles are not unique on a real board — "Untitled" alone accounted
    for several, and a slug collision silently means one card overwrites
    another's file and the mirror is quietly short. When a slug is claimed by
    more than one card in the same status dir, ALL of them get an id suffix,
    not just the losers: that keeps a card's filename from depending on which
    order the board came back in.
    """
    from .kanban.client import page_title

    keys: dict[str, list[str]] = {}
    base: dict[str, tuple[str, str]] = {}
    for card in cards:
        status_key = _status_dirname(_card_status(card), statuses)
        slug = _slugify(page_title(card) or "Untitled")
        base[card["id"]] = (status_key, slug)
        keys.setdefault(f"{status_key}/{slug}", []).append(card["id"])

    out: dict[str, str] = {}
    for page_id, (status_key, slug) in base.items():
        colliding = keys[f"{status_key}/{slug}"]
        out[page_id] = f"{slug}-{page_id.replace('-', '')[:8]}" if len(colliding) > 1 else slug
    return out


def _render_card(client: NotionClient, card: dict, statuses: dict[str, str],
                 *, with_comments: bool, slug: str | None = None) -> tuple[str, str, str]:
    """Render one card. Returns (rel_path, status_key, content)."""
    from .kanban.client import extract_property_value, page_title

    page_id = card["id"]
    title = page_title(card) or "Untitled"
    props = card.get("properties", {})
    plain = {k: extract_property_value(v) for k, v in props.items()}
    notion_status = plain.get("Status") or ""
    status_key = _status_dirname(notion_status, statuses)
    slug = slug or _slugify(title)
    rel_path = f"{KANBAN_SUBDIR}/{status_key}/{slug}.md"

    body = _blocks_to_md(client, _get_block_children(client, page_id))
    comments = _card_comments_md(client, page_id) if with_comments else ""

    sections = [f"# {title}", ""]
    meta = [f"- **{k}**: {v}" for k, v in sorted(plain.items())
            if v not in (None, "", [], False) and k not in ("Status",)]
    if meta:
        sections += ["## Propriedades", "", *meta, ""]
    sections += ["## Conteúdo", "", body or "*(card sem corpo)*", ""]
    if comments:
        sections += ["## Comentários", "", comments, ""]
    full_content = "\n".join(sections).strip()

    content = "\n".join([
        "---",
        "source: notion",
        "repo: kanban",
        f"path: {rel_path}",
        f"notion_id: {page_id}",
        f"status: {notion_status}",
        f"status_key: {status_key}",
        f"url: {card.get('url', '')}",
        f"checksum: {_sha256(full_content)}",
        f"last_edited: {card.get('last_edited_time', '')}",
        "---",
        "",
        full_content,
        "",
    ])
    return rel_path, status_key, content


def _query_all_cards(client: NotionClient, database_id: str) -> list[dict]:
    """Every card on the board, paginated. One query returns each card's
    properties AND its ``last_edited_time``, which is what makes the
    incremental path cheap: only cards whose timestamp moved need their body
    and comments fetched."""
    cards: list[dict] = []
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = client.query_database(database_id, body)
        cards.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return cards


def sync_kanban(client: NotionClient, board, *, force: bool = False,
                with_comments: bool = True) -> dict:
    """Mirror every Kanban card into ``notion/kanban/<status>/<slug>.md``.

    **This tree is derived, not a source.** Deleting a file here does nothing
    to Notion — unlike ``notion/notes/``, where ``bidirectional`` makes a
    local deletion archive the page. Cards are re-exported from the board on
    every run, so a hand-deleted file simply comes back. That asymmetry is
    deliberate: a Kanban card's lifecycle belongs to the board.

    Cards that changed status have their previous file removed, so a card is
    never in two status dirs at once.
    """
    db_id = board.config.database_id
    if not db_id:
        raise NotionError(503, "kanban_database_id is not configured — nothing to export")

    statuses = board.config.statuses
    root = kb_dir()
    state = _load_state()
    tracked: dict = state.get("kanban", {})
    lines: list[str] = []

    cards = _query_all_cards(client, db_id)
    active_ids = {c["id"] for c in cards}
    slugs = _card_slugs(cards, statuses)
    added = updated = skipped = moved = removed = 0

    # A card that left the board (archived/deleted) must not linger in the
    # mirror claiming a status it no longer has.
    for page_id, info in list(tracked.items()):
        if page_id in active_ids:
            continue
        old = os.path.join(root, info.get("path", ""))
        if info.get("path") and os.path.exists(old):
            os.remove(old)
            lines.append(f"removed (gone from board): {info['path']}")
            removed += 1
        del tracked[page_id]

    for card in cards:
        page_id = card["id"]
        prev = tracked.get(page_id, {})
        last_edited = card.get("last_edited_time", "")

        # Skip on an unchanged timestamp WITHOUT fetching the body/comments —
        # that's the whole point of reading last_edited_time off the query.
        expected = f"{KANBAN_SUBDIR}/{_status_dirname(_card_status(card), statuses)}/{slugs[page_id]}.md"
        if (not force and prev.get("last_edited") == last_edited
                and prev.get("path") == expected
                and os.path.exists(os.path.join(root, expected))):
            skipped += 1
            continue

        try:
            rel_path, status_key, content = _render_card(
                client, card, statuses, with_comments=with_comments,
                slug=slugs.get(page_id))
        except NotionError as exc:
            lines.append(f"warn: failed to render card {page_id} — {exc}")
            continue

        old_path = prev.get("path")
        if old_path and old_path != rel_path:
            old_abs = os.path.join(root, old_path)
            if os.path.exists(old_abs):
                os.remove(old_abs)
            lines.append(f"moved: {old_path} → {rel_path}")
            moved += 1

        out = os.path.join(root, rel_path)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        existed = os.path.exists(out)
        with open(out, "w") as f:
            f.write(content)
        if existed and not (old_path and old_path != rel_path):
            updated += 1
            lines.append(f"updated: {rel_path}")
        elif not existed:
            added += 1
            lines.append(f"added: {rel_path}")

        tracked[page_id] = {"path": rel_path, "status_key": status_key,
                            "last_edited": last_edited}

    _prune_empty_dirs(os.path.join(root, KANBAN_SUBDIR))
    state["kanban"] = tracked
    _save_state({**state, "last_sync": datetime.now(timezone.utc).isoformat()})

    by_status: dict[str, int] = {}
    for info in tracked.values():
        by_status[info.get("status_key", "?")] = by_status.get(info.get("status_key", "?"), 0) + 1
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped,
            "moved": moved, "removed": removed, "cards": len(tracked),
            "by_status": dict(sorted(by_status.items())), "log": lines,
            "kanban_dir": os.path.join(root, KANBAN_SUBDIR)}


# ── the sync ────────────────────────────────────────────────────────────

def sync_notes(client: NotionClient, root_page_id: str, *, force: bool = False,
               bidirectional: bool = False) -> dict:
    """Sync Notion pages under ``root_page_id`` into ``notion/notes/``.

    Returns ``{added, updated, skipped, deleted, notes_dir, log}``. ``log``
    carries the per-page lines the monolith printed to stdout — this runs
    inside the workspace server, where nobody would ever see a print, so the
    CLI renders them instead.

    A note whose tracked path differs from where it belongs now is moved, not
    duplicated. That is what migrates a workspace synced by v0.5.0 (which
    wrote a bare ``notes/``) onto ``notion/notes/`` without leaving 24 orphans
    behind for the KB to keep indexing.
    """
    if not root_page_id:
        raise NotionError(503, f"{ROOT_PAGE_KEY} is not configured — "
                               f"POST /api/apps/notion/config {{\"{ROOT_PAGE_KEY}\": \"...\"}}")

    root = kb_dir()
    target = notes_dir()
    os.makedirs(target, exist_ok=True)
    state = _load_state()
    tracked: dict = state.get("pages", {})
    lines: list[str] = []
    deleted = 0

    # Push: archive Notion pages whose local file was deleted.
    #
    # "Deleted" has to mean deleted from wherever the state says it was, not
    # just from where it belongs today. When MIRROR_SUBDIR changed in v0.6.0
    # every tracked note was still sitting under the OLD path — checking only
    # the new one would have read all 24 as locally deleted and archived them
    # in Notion on the first upgraded run.
    if bidirectional:
        for page_id, info in list(tracked.items()):
            slug = info.get("slug", "")
            candidates = [os.path.join(target, f"{slug}.md")]
            if info.get("path"):
                candidates.append(os.path.join(root, info["path"]))
            if any(os.path.exists(c) for c in candidates):
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
            for local in {os.path.join(target, f"{slug}.md"),
                          os.path.join(root, info.get("path") or "")}:
                if local and os.path.isfile(local):
                    os.remove(local)
                    lines.append(f"removed local (deleted in Notion): {slug}.md")
                    deleted += 1
                    break
            del tracked[page_id]

    added = updated = skipped = 0
    for block in child_pages:
        page_id = block["id"]
        rendered = _render_page(client, page_id)
        if rendered is None:
            lines.append(f"warn: failed to fetch page {page_id}")
            continue

        slug, last_edited, content = rendered
        rel_path = f"{NOTES_SUBDIR}/{slug}.md"
        out_path = os.path.join(target, f"{slug}.md")

        # A tracked note that used to live elsewhere (a renamed page, or the
        # v0.5.0 layout) is moved rather than duplicated. v0.5.0's state rows
        # carry no ``path`` at all, so fall back to where that version wrote:
        # without this the 24 notes it left in a bare ``notes/`` would stay
        # there forever, indexed alongside their own replacements.
        old_rel = tracked.get(page_id, {}).get("path") or f"notes/{slug}.md"
        if old_rel != rel_path:
            old_abs = os.path.join(root, old_rel)
            if os.path.isfile(old_abs):
                os.remove(old_abs)
                lines.append(f"moved: {old_rel} → {rel_path}")

        if os.path.exists(out_path) and not force:
            with open(out_path) as f:
                if f.read() == content:
                    tracked[page_id] = {"slug": slug, "path": rel_path,
                                        "last_edited": last_edited}
                    skipped += 1
                    continue
            updated += 1
            action = "updated"
        else:
            added += 1
            action = "added"

        with open(out_path, "w") as f:
            f.write(content)
        tracked[page_id] = {"slug": slug, "path": rel_path, "last_edited": last_edited}
        lines.append(f"{action}: {rel_path}  ({last_edited})")

    state["pages"] = tracked
    _save_state({**state, "last_sync": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped,
            "deleted": deleted, "notes_dir": target, "log": lines}


def run_sync(client: NotionClient, root_page_id: str, board=None, *,
             force: bool = False, bidirectional: bool = False, rebuild: bool = True,
             notes: bool = True, kanban: bool = True, with_comments: bool = True) -> dict:
    """Run both mirrors and reindex once at the end.

    One rebuild for both halves, not one each: reindexing is the expensive
    step and doing it twice per sync would double the cost of every run to no
    benefit.

    Either half can be skipped, and a half that isn't configured is *skipped*
    rather than fatal — a workspace using only the Kanban mirror shouldn't
    have to invent a ``sync_root_page_id`` to run the command.
    """
    result: dict = {"ok": True, "notes": None, "kanban": None, "kb_rebuild": None}
    log: list[str] = []
    changed = 0

    if notes:
        if not root_page_id:
            log.append(f"skipped notes: {ROOT_PAGE_KEY} is not configured")
        else:
            n = sync_notes(client, root_page_id, force=force, bidirectional=bidirectional)
            result["notes"] = n
            log += [f"notes/ {line}" for line in n["log"]]
            changed += n["added"] + n["updated"] + n["deleted"]

    if kanban:
        if board is None or not board.config.database_id:
            log.append("skipped kanban: kanban_database_id is not configured")
        else:
            k = sync_kanban(client, board, force=force, with_comments=with_comments)
            result["kanban"] = k
            log += [f"kanban/ {line}" for line in k["log"]]
            changed += k["added"] + k["updated"] + k["moved"] + k["removed"]

    if rebuild and changed:
        result["kb_rebuild"] = _rebuild_kb()
        log.append("KB index rebuilt" if result["kb_rebuild"]["ok"]
                   else f"warn: KB rebuild failed — {result['kb_rebuild'].get('error')}")

    result["log"] = log
    return result


def sync_state() -> dict:
    state = _load_state()
    kanban = state.get("kanban", {})
    by_status: dict[str, int] = {}
    for info in kanban.values():
        key = info.get("status_key", "?")
        by_status[key] = by_status.get(key, 0) + 1
    return {"last_sync": state.get("last_sync"),
            "tracked_pages": len(state.get("pages", {})),
            "tracked_cards": len(kanban),
            "cards_by_status": dict(sorted(by_status.items())),
            "notes_dir": notes_dir(), "kanban_dir": kanban_dir(),
            "state_file": state_path()}
