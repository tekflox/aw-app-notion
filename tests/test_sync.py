"""Coverage for notion_app/sync.py — the port of agentic-workspace's
``src/libs/notion_sync.py`` (``./aw notion-sync``).

Notion access goes through NotionClient.request, so the same fake-client
approach as tests/test_kanban.py covers the whole engine offline. The KB
rebuild is monkeypatched — it is an HTTP call to a sibling container.

Run: .venv/aw/bin/python -m pytest tests/test_sync.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notion_app import sync as sync_mod  # noqa: E402
from notion_app.kanban.client import NotionError  # noqa: E402

ROOT_PAGE = "root-page-1"


class FakeClient:
    def __init__(self, pages: dict, children: dict):
        self.pages = pages
        self.children = children
        self.patched: list[tuple[str, dict]] = []

    def request(self, method, path, body=None):
        if method == "PATCH" and path.startswith("/pages/"):
            self.patched.append((path.split("/")[-1], body))
            return {}
        if method == "GET" and path.startswith("/pages/"):
            page_id = path.split("/")[-1]
            if page_id not in self.pages:
                raise NotionError(404, '{"code":"object_not_found"}')
            return self.pages[page_id]
        if method == "GET" and path.startswith("/blocks/"):
            block_id = path.split("/")[2]
            return {"results": self.children.get(block_id, []), "has_more": False}
        raise AssertionError(f"unexpected {method} {path}")


def _page(page_id, title, last_edited="2026-08-13T00:00:00.000Z"):
    return {"id": page_id, "last_edited_time": last_edited,
            "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}}}


def _para(text):
    return {"id": f"b-{text}", "type": "paragraph",
            "paragraph": {"rich_text": [{"plain_text": text, "annotations": {}}]}}


def _child_page(page_id, title):
    return {"id": page_id, "type": "child_page", "child_page": {"title": title}}


def _setup(tmp_path, monkeypatch, titles=("Nota Um", "Nota Dois")):
    """Point the module's two on-disk locations at a tmp dir. They're derived
    from AW_WORKSPACE_HOME, so setting that covers both."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    monkeypatch.setattr(sync_mod, "_rebuild_kb", lambda: {"ok": True, "status": 200})

    pages, children = {}, {ROOT_PAGE: []}
    for i, title in enumerate(titles):
        pid = f"page-{i}"
        pages[pid] = _page(pid, title)
        children[ROOT_PAGE].append(_child_page(pid, title))
        children[pid] = [_para(f"conteúdo de {title}")]
    return FakeClient(pages, children)


def _notes(tmp_path):
    d = tmp_path / "knowledge_base" / "notion" / "notes"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _note(tmp_path, name):
    return tmp_path / "knowledge_base" / "notion" / "notes" / name


# ── paths ───────────────────────────────────────────────────────────────

def test_notes_land_in_the_kb_tree_not_the_app_dir(tmp_path, monkeypatch):
    """The monolith wrote into its own repo. Here the KB is a separate
    container whose indexed tree is <workspace_home>/knowledge_base — writing
    anywhere else produces notes nothing ever indexes."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", "/srv/home")
    assert sync_mod.notes_dir() == "/srv/home/knowledge_base/notion/notes"
    assert sync_mod.kanban_dir() == "/srv/home/knowledge_base/notion/kanban"
    assert sync_mod.state_path() == "/srv/home/data/notion/notion_sync_state.json"


# ── pull ────────────────────────────────────────────────────────────────

def test_sync_writes_a_note_per_child_page(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    result = sync_mod.sync_notes(client, ROOT_PAGE)
    assert result["added"] == 2
    assert result["updated"] == result["skipped"] == 0
    assert _notes(tmp_path) == ["nota-dois.md", "nota-um.md"]


def test_note_carries_the_monoliths_frontmatter(tmp_path, monkeypatch):
    """Keeping source/repo/path/notion_id/checksum/last_edited identical is
    what lets an already-indexed note keep its identity across the move."""
    client = _setup(tmp_path, monkeypatch, titles=("Nota Um",))
    sync_mod.sync_notes(client, ROOT_PAGE)
    text = (_note(tmp_path, "nota-um.md")).read_text()
    assert text.startswith("---\nsource: notion\nrepo: notes\npath: notion/notes/nota-um.md\n")
    assert "notion_id: page-0" in text
    assert "checksum: " in text
    assert "# Nota Um" in text
    assert "conteúdo de Nota Um" in text


def test_unchanged_pages_are_skipped_on_a_second_run(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.sync_notes(client, ROOT_PAGE)
    second = sync_mod.sync_notes(client, ROOT_PAGE)
    assert second["skipped"] == 2
    assert second["added"] == second["updated"] == 0


def test_force_rewrites_everything(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.sync_notes(client, ROOT_PAGE)
    forced = sync_mod.sync_notes(client, ROOT_PAGE, force=True)
    assert forced["added"] == 2
    assert forced["skipped"] == 0


def test_an_edited_page_counts_as_updated(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, titles=("Nota Um",))
    sync_mod.sync_notes(client, ROOT_PAGE)
    client.children["page-0"] = [_para("texto novo")]
    result = sync_mod.sync_notes(client, ROOT_PAGE)
    assert result["updated"] == 1
    assert "texto novo" in (_note(tmp_path, "nota-um.md")).read_text()


def test_one_unreadable_page_does_not_abort_the_others(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    del client.pages["page-0"]  # 404s on fetch
    result = sync_mod.sync_notes(client, ROOT_PAGE)
    assert result["added"] == 1
    assert any("failed to fetch" in line for line in result["log"])


def test_state_records_every_synced_page(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.sync_notes(client, ROOT_PAGE)
    state = json.loads(Path(sync_mod.state_path()).read_text())
    assert set(state["pages"]) == {"page-0", "page-1"}
    assert state["pages"]["page-0"]["slug"] == "nota-um"
    assert state["last_sync"]


def test_missing_root_page_id_is_a_503_not_a_crash(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    try:
        sync_mod.sync_notes(client, "")
    except NotionError as exc:
        assert exc.status == 503
        assert "sync_root_page_id" in str(exc)
    else:
        raise AssertionError("expected NotionError")


# ── push (bidirectional) ────────────────────────────────────────────────

def test_pull_only_never_archives_anything(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.sync_notes(client, ROOT_PAGE)
    (_note(tmp_path, "nota-um.md")).unlink()
    result = sync_mod.sync_notes(client, ROOT_PAGE, bidirectional=False)
    assert result["deleted"] == 0
    assert client.patched == []


def test_deleting_a_note_archives_its_notion_page(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.sync_notes(client, ROOT_PAGE, bidirectional=True)
    (_note(tmp_path, "nota-um.md")).unlink()

    # the page is gone from Notion's side too once archived
    client.children[ROOT_PAGE] = [b for b in client.children[ROOT_PAGE] if b["id"] != "page-0"]
    result = sync_mod.sync_notes(client, ROOT_PAGE, bidirectional=True)
    assert client.patched == [("page-0", {"archived": True})]
    assert result["deleted"] == 1
    state = json.loads(Path(sync_mod.state_path()).read_text())
    assert "page-0" not in state["pages"]


def test_a_page_archived_in_notion_removes_the_local_note(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.sync_notes(client, ROOT_PAGE, bidirectional=True)
    client.children[ROOT_PAGE] = [b for b in client.children[ROOT_PAGE] if b["id"] != "page-1"]

    result = sync_mod.sync_notes(client, ROOT_PAGE, bidirectional=True)
    assert result["deleted"] == 1
    assert _notes(tmp_path) == ["nota-um.md"]


# ── KB rebuild ──────────────────────────────────────────────────────────

def test_rebuild_is_skipped_when_nothing_changed(tmp_path, monkeypatch):
    calls = []
    client = _setup(tmp_path, monkeypatch)
    sync_mod.run_sync(client, ROOT_PAGE, None, kanban=False)
    monkeypatch.setattr(sync_mod, "_rebuild_kb", lambda: calls.append(1) or {"ok": True})
    result = sync_mod.run_sync(client, ROOT_PAGE, None, kanban=False)
    assert calls == []
    assert result["kb_rebuild"] is None


def test_no_rebuild_writes_the_notes_anyway(tmp_path, monkeypatch):
    calls = []
    client = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sync_mod, "_rebuild_kb", lambda: calls.append(1) or {"ok": True})
    result = sync_mod.run_sync(client, ROOT_PAGE, None, rebuild=False, kanban=False)
    assert calls == []
    assert result["notes"]["added"] == 2
    assert _notes(tmp_path) == ["nota-dois.md", "nota-um.md"]


def test_a_failed_rebuild_still_reports_the_written_notes(tmp_path, monkeypatch):
    """Best-effort by design: the notes are on disk, the next build picks them
    up — but it must not read as a clean run either."""
    client = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sync_mod, "_rebuild_kb",
                        lambda: {"ok": False, "status": 0, "error": "connection refused"})
    result = sync_mod.run_sync(client, ROOT_PAGE, None, kanban=False)
    assert result["notes"]["added"] == 2
    assert result["kb_rebuild"]["ok"] is False
    assert any("KB rebuild failed" in line for line in result["log"])
    assert _notes(tmp_path) == ["nota-dois.md", "nota-um.md"]


# ── markdown conversion ─────────────────────────────────────────────────

def test_slugify_matches_the_monolith():
    """Accents survive (Python's \\w is Unicode-aware) — kept deliberately
    identical to the monolith's regex, because the slug IS the filename and
    "improving" it here would orphan every note already synced."""
    assert sync_mod._slugify("Notas de Reunião — 2026!") == "notas-de-reunião-2026"
    assert sync_mod._slugify("!!!") == "untitled"


def test_annotations_become_markdown():
    rt = [{"plain_text": "bold", "annotations": {"bold": True}},
          {"plain_text": " and ", "annotations": {}},
          {"plain_text": "code", "annotations": {"code": True}}]
    assert sync_mod._rich_text_to_md(rt) == "**bold** and `code`"


def test_empty_page_gets_a_placeholder_body(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, titles=("Vazia",))
    client.children["page-0"] = []
    sync_mod.sync_notes(client, ROOT_PAGE)
    assert "*(página vazia)*" in (_note(tmp_path, "vazia.md")).read_text()


# ── kanban mirror ───────────────────────────────────────────────────────

class FakeBoardClient(FakeClient):
    """Adds database-query + comments on top of the page/block fakes."""

    def __init__(self, cards, children, comments=None):
        super().__init__({}, children)
        self.cards = cards
        self.comments = comments or {}
        self.body_fetches: list[str] = []

    def request(self, method, path, body=None):
        if method == "GET" and path.startswith("/comments?"):
            import urllib.parse
            q = urllib.parse.parse_qs(path.split("?", 1)[1])
            return {"results": self.comments.get(q["block_id"][0], [])}
        if method == "GET" and path.startswith("/blocks/"):
            self.body_fetches.append(path.split("/")[2])
        return super().request(method, path, body)

    def query_database(self, database_id, body):
        return {"results": self.cards, "has_more": False}


class FakeBoard:
    def __init__(self, statuses, database_id="db-1"):
        self.config = type("C", (), {"database_id": database_id, "statuses": statuses})()


STATUSES = {"backlog": "Backlog", "done": "Done", "need_human": "Need Human",
            "ready": "Ready", "auto_resolved": "Self-closed"}


def _card(page_id, title, status, last_edited="2026-08-13T00:00:00.000Z", **props):
    properties = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
        "Status": {"type": "select", "select": {"name": status} if status else None},
    }
    for k, v in props.items():
        properties[k] = {"type": "rich_text", "rich_text": [{"plain_text": v}]}
    return {"id": page_id, "last_edited_time": last_edited,
            "url": f"https://notion.so/{page_id}", "properties": properties}


def _kanban_setup(tmp_path, monkeypatch, cards):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    children = {c["id"]: [_para(f"corpo de {c['id']}")] for c in cards}
    return FakeBoardClient(cards, children), FakeBoard(STATUSES)


def _kanban_dir(tmp_path):
    return tmp_path / "knowledge_base" / "notion" / "kanban"


def _tree(tmp_path):
    """The flat kanban mirror (§19): just the filenames directly under
    notion/kanban/ — there is no status subdirectory to key on anymore."""
    root = _kanban_dir(tmp_path)
    if not root.is_dir():
        return []
    return sorted(f.name for f in root.iterdir() if f.is_file())


def _legacy_card(tmp_path, slug, status_key, content="legacy content\n"):
    """Write a file at the pre-§19 status-dir path and return that rel path
    — used to seed a card already tracked under the old layout."""
    rel = f"notion/kanban/{status_key}/{slug}.md"
    path = tmp_path / "knowledge_base" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return rel


def _seed_legacy_state(tmp_path, entries: dict):
    """entries: page_id -> {"path", "status_key", "last_edited"}. Writes the
    kanban section of the state file directly, as if a pre-§19 run had
    produced it."""
    state_file = Path(sync_mod.state_path())
    state_file.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if state_file.exists():
        existing = json.loads(state_file.read_text())
    existing["kanban"] = entries
    state_file.write_text(json.dumps(existing))


def test_cards_land_flat_no_status_directory(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [
        _card("c1", "Primeiro", "Backlog"),
        _card("c2", "Segundo", "Done"),
        _card("c3", "Terceiro", "Need Human"),
    ])
    result = sync_mod.sync_kanban(client, board)
    assert result["added"] == 3
    assert _tree(tmp_path) == ["primeiro.md", "segundo.md", "terceiro.md"]
    assert result["by_status"] == {"backlog": 1, "done": 1, "need_human": 1}
    assert all(not (_kanban_dir(tmp_path) / name).is_dir()
              for name in os.listdir(_kanban_dir(tmp_path)))


def test_card_markdown_carries_status_in_frontmatter_not_the_path(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch,
                                  [_card("c1", "Primeiro", "Done", Source="system-analyst")])
    client.comments = {"c1": [{"created_time": "2026-08-01T00:00:00.000Z",
                               "rich_text": [{"plain_text": "entregue"}]}]}
    sync_mod.sync_kanban(client, board)
    text = (_kanban_dir(tmp_path) / "primeiro.md").read_text()
    assert "path: notion/kanban/primeiro.md" in text
    assert "status: Done" in text and "status_key: done" in text
    assert "notion_id: c1" in text
    assert "- **Source**: system-analyst" in text
    assert "corpo de c1" in text
    assert "## Comentários" in text and "entregue" in text


def test_a_status_with_no_logical_key_still_resolves(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch,
                                  [_card("c1", "Estranho", "Em Revisão")])
    sync_mod.sync_kanban(client, board)
    text = (_kanban_dir(tmp_path) / "estranho.md").read_text()
    assert "status_key: em-revisão" in text


def test_a_card_with_no_status_is_not_dropped(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Sem", None)])
    sync_mod.sync_kanban(client, board)
    text = (_kanban_dir(tmp_path) / "sem.md").read_text()
    assert "status_key: no-status" in text


def test_comments_can_be_turned_off(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    client.comments = {"c1": [{"created_time": "x", "rich_text": [{"plain_text": "oi"}]}]}
    sync_mod.sync_kanban(client, board, with_comments=False)
    text = (_kanban_dir(tmp_path) / "primeiro.md").read_text()
    assert "## Comentários" not in text


def test_unchanged_cards_skip_without_fetching_their_body(tmp_path, monkeypatch):
    """The whole point of reading last_edited_time off the board query: an
    unchanged card must cost zero extra Notion calls."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    sync_mod.sync_kanban(client, board)
    client.body_fetches.clear()

    second = sync_mod.sync_kanban(client, board)
    assert second["skipped"] == 1
    assert second["added"] == second["updated"] == 0
    assert client.body_fetches == []


def test_force_re_renders_even_unchanged_cards(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    sync_mod.sync_kanban(client, board)
    client.body_fetches.clear()
    forced = sync_mod.sync_kanban(client, board, force=True)
    assert forced["updated"] == 1
    assert client.body_fetches == ["c1"]


def test_a_column_move_rewrites_the_same_path_not_moved_plus_removed(tmp_path, monkeypatch):
    """§19.7's first acceptance gate: once a card is on the flat layout, a
    column move is a content update at the SAME path — the state-file diff
    must show `updated`, never `moved`+`removed`."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Backlog")])
    sync_mod.sync_kanban(client, board)
    assert _tree(tmp_path) == ["primeiro.md"]

    client.cards = [_card("c1", "Primeiro", "Done", last_edited="2026-08-14T00:00:00.000Z")]
    result = sync_mod.sync_kanban(client, board)
    assert result["updated"] == 1
    assert result["moved"] == 0
    assert result["migrated"] == 0
    assert any(line == "updated: notion/kanban/primeiro.md" for line in result["log"])
    assert _tree(tmp_path) == ["primeiro.md"]
    text = (_kanban_dir(tmp_path) / "primeiro.md").read_text()
    assert "status: Done" in text


def test_archived_statuses_land_flat_too(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Backlog")])
    sync_mod.sync_kanban(client, board)

    client.cards = [_card("c1", "Primeiro", "Archived", last_edited="2026-08-14T00:00:00.000Z")]
    result = sync_mod.sync_kanban(client, board)
    assert result["updated"] == 1
    assert _tree(tmp_path) == ["primeiro.md"]


def test_a_card_gone_from_the_board_is_removed_from_the_mirror(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [
        _card("c1", "Primeiro", "Done"), _card("c2", "Segundo", "Done")])
    sync_mod.sync_kanban(client, board)

    client.cards = [c for c in client.cards if c["id"] == "c2"]
    result = sync_mod.sync_kanban(client, board)
    assert result["removed"] == 1
    assert _tree(tmp_path) == ["segundo.md"]


def test_the_kanban_mirror_never_writes_back_to_notion(tmp_path, monkeypatch):
    """Deleting a file under kanban/ must not archive the card — unlike
    notes/, this tree is derived. The file simply comes back."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    sync_mod.sync_kanban(client, board)
    (_kanban_dir(tmp_path) / "primeiro.md").unlink()

    result = sync_mod.sync_kanban(client, board)
    assert client.patched == []          # nothing archived
    assert result["added"] == 1          # just re-exported
    assert _tree(tmp_path) == ["primeiro.md"]


def test_missing_database_id_is_a_503(tmp_path, monkeypatch):
    client, _ = _kanban_setup(tmp_path, monkeypatch, [])
    try:
        sync_mod.sync_kanban(client, FakeBoard(STATUSES, database_id=""))
    except NotionError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("expected NotionError")


# ── §19 migration: flatten legacy status-dir cards ─────────────────────

def test_a_legacy_card_is_left_untouched_when_the_flag_is_off(tmp_path, monkeypatch):
    """The default. A workspace upgrading this app must not wake up to its
    entire backlog flattened in one scan — that is exactly the bulk delete
    the §7 valve exists to refuse."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Backlog")])
    rel = _legacy_card(tmp_path, "primeiro", "backlog")
    _seed_legacy_state(tmp_path, {"c1": {"path": rel, "status_key": "backlog",
                                         "last_edited": "2026-08-13T00:00:00.000Z"}})

    result = sync_mod.sync_kanban(client, board)
    assert result["migrated"] == 0
    assert result["skipped"] == 1
    assert (tmp_path / "knowledge_base" / rel).exists()        # still on the old path
    assert not (_kanban_dir(tmp_path) / "primeiro.md").exists()  # not flattened


def test_flag_on_flattens_a_legacy_card_old_removed_new_written_same_run(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Backlog")])
    rel = _legacy_card(tmp_path, "primeiro", "backlog")
    _seed_legacy_state(tmp_path, {"c1": {"path": rel, "status_key": "backlog",
                                         "last_edited": "2026-08-13T00:00:00.000Z"}})

    result = sync_mod.sync_kanban(client, board, flatten_migration=True)
    assert result["migrated"] == 1
    assert not (tmp_path / "knowledge_base" / rel).exists()
    assert _tree(tmp_path) == ["primeiro.md"]
    state = json.loads(Path(sync_mod.state_path()).read_text())
    assert state["kanban"]["c1"]["path"] == "notion/kanban/primeiro.md"


def test_migration_batch_is_capped_per_run(tmp_path, monkeypatch):
    cards = [_card(f"c{i}", f"Card {i}", "Backlog") for i in range(5)]
    client, board = _kanban_setup(tmp_path, monkeypatch, cards)
    entries = {}
    for c in cards:
        rel = _legacy_card(tmp_path, f"card-{c['id'][1:]}", "backlog")
        entries[c["id"]] = {"path": rel, "status_key": "backlog",
                            "last_edited": "2026-08-13T00:00:00.000Z"}
    _seed_legacy_state(tmp_path, entries)

    result = sync_mod.sync_kanban(client, board, flatten_migration=True, flatten_batch_limit=2)
    assert result["migrated"] == 2
    assert len(_tree(tmp_path)) == 2
    assert result["skipped"] == 3

    # the rest converge over the following runs, same budget each time
    second = sync_mod.sync_kanban(client, board, flatten_migration=True, flatten_batch_limit=2)
    assert second["migrated"] == 2
    third = sync_mod.sync_kanban(client, board, flatten_migration=True, flatten_batch_limit=2)
    assert third["migrated"] == 1
    assert len(_tree(tmp_path)) == 5


def test_migration_is_idempotent_across_a_simulated_crash(tmp_path, monkeypatch):
    """The state file is flushed after every migrated card (not just once at
    the end), so a crash mid-batch leaves disk and state consistent — the
    next run picks up exactly where it left off, no duplicates, no errors."""
    cards = [_card(f"c{i}", f"Card {i}", "Backlog") for i in range(3)]
    client, board = _kanban_setup(tmp_path, monkeypatch, cards)
    entries = {}
    for c in cards:
        rel = _legacy_card(tmp_path, f"card-{c['id'][1:]}", "backlog")
        entries[c["id"]] = {"path": rel, "status_key": "backlog",
                            "last_edited": "2026-08-13T00:00:00.000Z"}
    _seed_legacy_state(tmp_path, entries)

    # Simulate a crash: migrate the first card for real, then hand-roll what
    # a crash right after would leave behind — disk already flattened for
    # c0, state already reflecting it (sync_kanban flushes per migrated
    # card), but the run never reached c1/c2.
    first = sync_mod.sync_kanban(client, board, flatten_migration=True, flatten_batch_limit=1)
    assert first["migrated"] == 1
    state_after_crash = json.loads(Path(sync_mod.state_path()).read_text())
    assert sum(1 for v in state_after_crash["kanban"].values()
              if not sync_mod._is_legacy_kanban_path(v["path"])) == 1

    # Re-run picks up the remaining two cleanly — no duplicate files, no
    # exceptions, no re-processing of the already-flattened one.
    second = sync_mod.sync_kanban(client, board, flatten_migration=True, flatten_batch_limit=10)
    assert second["migrated"] == 2
    assert len(_tree(tmp_path)) == 3
    assert len(set(_tree(tmp_path))) == 3  # no duplicates


def test_stale_dual_folder_leftovers_are_removed_on_migration(tmp_path, monkeypatch):
    """The known bug (12 live cards): the status file a card moved OUT of was
    never cleaned up, so the same slug exists in two status dirs. Flattening
    must converge both into the single new flat file, absorbing the leftover
    rather than leaving it to rot."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    current_rel = _legacy_card(tmp_path, "primeiro", "done", content="current\n")
    stale_rel = _legacy_card(tmp_path, "primeiro", "backlog", content="stale leftover\n")
    _seed_legacy_state(tmp_path, {"c1": {"path": current_rel, "status_key": "done",
                                         "last_edited": "2026-08-13T00:00:00.000Z"}})
    assert (tmp_path / "knowledge_base" / stale_rel).exists()

    result = sync_mod.sync_kanban(client, board, flatten_migration=True)
    assert result["migrated"] == 1
    assert not (tmp_path / "knowledge_base" / current_rel).exists()
    assert not (tmp_path / "knowledge_base" / stale_rel).exists()
    assert _tree(tmp_path) == ["primeiro.md"]


def test_convergence_leaves_no_status_subdirectory_on_disk(tmp_path, monkeypatch):
    cards = [_card("c1", "Primeiro", "Backlog"), _card("c2", "Segundo", "Done")]
    client, board = _kanban_setup(tmp_path, monkeypatch, cards)
    entries = {
        "c1": {"path": _legacy_card(tmp_path, "primeiro", "backlog"),
               "status_key": "backlog", "last_edited": "2026-08-13T00:00:00.000Z"},
        "c2": {"path": _legacy_card(tmp_path, "segundo", "done"),
               "status_key": "done", "last_edited": "2026-08-13T00:00:00.000Z"},
    }
    _seed_legacy_state(tmp_path, entries)

    sync_mod.sync_kanban(client, board, flatten_migration=True)
    kanban_root = _kanban_dir(tmp_path)
    assert all(not (kanban_root / name).is_dir() for name in os.listdir(kanban_root))
    assert sorted(f.name for f in kanban_root.iterdir()) == ["primeiro.md", "segundo.md"]


# ── the two halves together ─────────────────────────────────────────────

def test_run_sync_skips_an_unconfigured_half_instead_of_failing(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    monkeypatch.setattr(sync_mod, "_rebuild_kb", lambda: {"ok": True})
    result = sync_mod.run_sync(client, "", board)     # no root_page_id
    assert result["notes"] is None
    assert result["kanban"]["added"] == 1
    assert any("skipped notes" in line for line in result["log"])


def test_run_sync_rebuilds_once_for_both_halves(tmp_path, monkeypatch):
    calls = []
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    client.pages = {"page-0": _page("page-0", "Nota Um")}
    client.children[ROOT_PAGE] = [_child_page("page-0", "Nota Um")]
    client.children["page-0"] = [_para("conteúdo")]
    monkeypatch.setattr(sync_mod, "_rebuild_kb", lambda: calls.append(1) or {"ok": True})

    result = sync_mod.run_sync(client, ROOT_PAGE, board)
    assert calls == [1]
    assert result["notes"]["added"] == 1
    assert result["kanban"]["added"] == 1


def test_state_keeps_both_halves(tmp_path, monkeypatch):
    """sync_notes and sync_kanban write the same state file — one must not
    clobber the other's section."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    client.pages = {"page-0": _page("page-0", "Nota Um")}
    client.children[ROOT_PAGE] = [_child_page("page-0", "Nota Um")]
    client.children["page-0"] = [_para("conteúdo")]
    monkeypatch.setattr(sync_mod, "_rebuild_kb", lambda: {"ok": True})

    sync_mod.run_sync(client, ROOT_PAGE, board)
    state = json.loads(Path(sync_mod.state_path()).read_text())
    assert set(state["pages"]) == {"page-0"}
    assert set(state["kanban"]) == {"c1"}


def test_v050_notes_are_migrated_out_of_the_bare_notes_dir(tmp_path, monkeypatch):
    """v0.5.0 wrote knowledge_base/notes/ and its state rows have no `path`.
    Upgrading must MOVE those files, not leave the old copies indexed next to
    their replacements."""
    client = _setup(tmp_path, monkeypatch, titles=("Nota Um",))
    legacy_dir = tmp_path / "knowledge_base" / "notes"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "nota-um.md").write_text("stale v0.5.0 copy")
    state_file = Path(sync_mod.state_path())
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"pages": {"page-0": {
        "slug": "nota-um", "last_edited": "2026-01-01T00:00:00.000Z"}}}))

    result = sync_mod.sync_notes(client, ROOT_PAGE)
    assert not (legacy_dir / "nota-um.md").exists()
    assert _notes(tmp_path) == ["nota-um.md"]
    assert any("moved: notes/nota-um.md" in line for line in result["log"])


def test_upgrading_with_bidirectional_does_not_archive_everything(tmp_path, monkeypatch):
    """The v0.5.0 → v0.6.0 layout change left every tracked note under the OLD
    path. Checking only the new path would read all of them as locally deleted
    and archive them in Notion — silent, irreversible data loss."""
    client = _setup(tmp_path, monkeypatch, titles=("Nota Um",))
    legacy_dir = tmp_path / "knowledge_base" / "notes"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "nota-um.md").write_text("stale v0.5.0 copy")
    state_file = Path(sync_mod.state_path())
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"pages": {"page-0": {
        "slug": "nota-um", "path": "notes/nota-um.md",
        "last_edited": "2026-01-01T00:00:00.000Z"}}}))

    sync_mod.sync_notes(client, ROOT_PAGE, bidirectional=True)
    assert client.patched == []
    assert _notes(tmp_path) == ["nota-um.md"]


# ── the background job ──────────────────────────────────────────────────

def test_sync_job_runs_off_the_request_and_reports_its_result():
    """The BYOD tunnel answers 502 for a long-held request while the work is
    still running fine, so the sync must not be awaited inside the POST."""
    import asyncio
    from notion_app.job import SyncJob

    async def scenario():
        job = SyncJob()
        assert job.snapshot()["status"] == "idle"
        assert job.start(lambda: {"ok": True, "added": 3}) is True
        assert job.snapshot()["status"] == "running"
        for _ in range(200):
            if job.snapshot()["status"] != "running":
                break
            await asyncio.sleep(0.01)
        return job.snapshot()

    final = asyncio.run(scenario())
    assert final["status"] == "done"
    assert final["result"] == {"ok": True, "added": 3}
    assert final["started_at"] and final["finished_at"]


def test_a_second_sync_is_refused_not_queued():
    """Two concurrent syncs would race on the same files and the same state
    document, and there's no reason to want one."""
    import asyncio
    import threading
    from notion_app.job import SyncJob

    release = threading.Event()

    async def scenario():
        job = SyncJob()
        assert job.start(lambda: release.wait(5) or {"ok": True}) is True
        second = job.start(lambda: {"ok": True})
        release.set()
        return second

    assert asyncio.run(scenario()) is False


def test_a_raising_job_is_reported_as_failed_not_lost():
    import asyncio
    from notion_app.job import SyncJob

    def boom():
        raise NotionError(404, '{"code":"object_not_found"}')

    async def scenario():
        job = SyncJob()
        job.start(boom)
        for _ in range(200):
            if job.snapshot()["status"] != "running":
                break
            await asyncio.sleep(0.01)
        return job.snapshot()

    final = asyncio.run(scenario())
    assert final["status"] == "failed"
    assert "object_not_found" in final["error"]
    assert final["result"] is None


def test_cards_with_the_same_title_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """A real board has repeated titles — "Untitled" alone accounted for
    several. Without disambiguation one card silently overwrites another's
    file and the mirror is quietly short."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [
        _card("c1", "Untitled", "Done"),
        _card("c2", "Untitled", "Done"),
        _card("c3", "Único", "Done"),
    ])
    result = sync_mod.sync_kanban(client, board)
    files = _tree(tmp_path)
    assert len(files) == 3
    assert "único.md" in files                       # unique title keeps a clean name
    assert len([f for f in files if f.startswith("untitled-")]) == 2
    assert result["added"] == 3
    assert result["cards"] == 3


def test_collisions_suffix_every_colliding_card_not_just_the_losers(tmp_path, monkeypatch):
    """Suffixing only the second one would make a card's filename depend on
    the order the board came back in."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [
        _card("3645bf3b-9510-8000-0000-00000000aaaa", "Untitled", "Done"),
        _card("3645bf3b-9510-8000-0000-00000000bbbb", "Untitled", "Done"),
    ])
    sync_mod.sync_kanban(client, board)
    assert _tree(tmp_path) == ["untitled-0000aaaa.md", "untitled-0000bbbb.md"]


def test_same_title_in_different_statuses_now_needs_a_suffix_board_wide(tmp_path, monkeypatch):
    """§19: status no longer separates cards into different directories, so
    two cards sharing a title now collide board-wide regardless of status —
    this is exactly the shape of the 12 known stale cross-status basename
    duplicates the migration absorbs."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [
        _card("3645bf3b-9510-8000-0000-00000000aaaa", "Deploy", "Done"),
        _card("3645bf3b-9510-8000-0000-00000000bbbb", "Deploy", "Backlog")])
    sync_mod.sync_kanban(client, board)
    assert _tree(tmp_path) == ["deploy-0000aaaa.md", "deploy-0000bbbb.md"]


def test_a_card_whose_slug_gained_a_suffix_is_moved_not_duplicated(tmp_path, monkeypatch):
    a = "3645bf3b-9510-8000-0000-00000000aaaa"
    b = "3645bf3b-9510-8000-0000-00000000bbbb"
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card(a, "Untitled", "Done")])
    sync_mod.sync_kanban(client, board)
    assert _tree(tmp_path) == ["untitled.md"]

    # a second card with the same title shows up → both must gain a suffix
    client.cards.append(_card(b, "Untitled", "Done"))
    client.children[b] = [_para("corpo")]
    sync_mod.sync_kanban(client, board)
    assert _tree(tmp_path) == ["untitled-0000aaaa.md", "untitled-0000bbbb.md"]


def test_tracked_card_count_matches_files_on_disk(tmp_path, monkeypatch):
    """The regression this whole group exists for: 469 cards tracked, 462 on
    disk, and nothing said a word."""
    cards = [_card(f"c{i}", "Untitled" if i % 3 == 0 else f"Card {i}", "Done")
             for i in range(12)]
    client, board = _kanban_setup(tmp_path, monkeypatch, cards)
    result = sync_mod.sync_kanban(client, board)
    on_disk = len(_tree(tmp_path))
    assert result["cards"] == on_disk == 12


def test_the_suffix_comes_from_the_id_tail_not_its_shared_prefix(tmp_path, monkeypatch):
    """Notion ids minted in one workspace share a leading prefix. A
    head-based suffix disambiguated nothing — three cards titled "AW Meta
    Display — aprovar deploy TestFlight" all became `...-3a45bf3b.md` and
    overwrote each other exactly like the un-suffixed name did."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [
        _card("3a45bf3b-9510-81a2-0000-000000000001", "Mesmo Título", "Done"),
        _card("3a45bf3b-9510-81a2-0000-000000000002", "Mesmo Título", "Done"),
        _card("3a45bf3b-9510-81a2-0000-000000000003", "Mesmo Título", "Done"),
    ])
    result = sync_mod.sync_kanban(client, board)
    assert len(_tree(tmp_path)) == 3
    assert result["cards"] == 3
