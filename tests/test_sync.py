"""Coverage for notion_app/sync.py — the port of agentic-workspace's
``src/libs/notion_sync.py`` (``./aw notion-sync``).

Notion access goes through NotionClient.request, so the same fake-client
approach as tests/test_kanban.py covers the whole engine offline. The KB
rebuild is monkeypatched — it is an HTTP call to a sibling container.

Run: .venv/aw/bin/python -m pytest tests/test_sync.py
"""
from __future__ import annotations

import json
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


def _tree(tmp_path):
    root = tmp_path / "knowledge_base" / "notion" / "kanban"
    if not root.is_dir():
        return {}
    return {d.name: sorted(f.name for f in d.iterdir())
            for d in sorted(root.iterdir()) if d.is_dir()}


def test_cards_are_filed_under_their_status_dir(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [
        _card("c1", "Primeiro", "Backlog"),
        _card("c2", "Segundo", "Done"),
        _card("c3", "Terceiro", "Need Human"),
    ])
    result = sync_mod.sync_kanban(client, board)
    assert result["added"] == 3
    assert _tree(tmp_path) == {
        "backlog": ["primeiro.md"],
        "done": ["segundo.md"],
        "need_human": ["terceiro.md"],
    }
    assert result["by_status"] == {"backlog": 1, "done": 1, "need_human": 1}


def test_dirs_use_the_logical_key_not_the_notion_label(tmp_path, monkeypatch):
    """'Auto-resolvido' → 'Self-closed' already happened once. Keying the
    tree on the Notion label would have moved every card on a rename."""
    client, board = _kanban_setup(tmp_path, monkeypatch,
                                  [_card("c1", "Fechado", "Self-closed")])
    sync_mod.sync_kanban(client, board)
    assert list(_tree(tmp_path)) == ["auto_resolved"]


def test_a_status_with_no_logical_key_still_gets_a_dir(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch,
                                  [_card("c1", "Estranho", "Em Revisão")])
    sync_mod.sync_kanban(client, board)
    assert list(_tree(tmp_path)) == ["em-revisão"]


def test_a_card_with_no_status_is_not_dropped(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Sem", None)])
    sync_mod.sync_kanban(client, board)
    assert list(_tree(tmp_path)) == ["no-status"]


def test_card_markdown_carries_frontmatter_body_and_comments(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch,
                                  [_card("c1", "Primeiro", "Done", Source="system-analyst")])
    client.comments = {"c1": [{"created_time": "2026-08-01T00:00:00.000Z",
                               "rich_text": [{"plain_text": "entregue"}]}]}
    sync_mod.sync_kanban(client, board)
    text = (tmp_path / "knowledge_base" / "notion" / "kanban" / "done" / "primeiro.md").read_text()
    assert "path: notion/kanban/done/primeiro.md" in text
    assert "status: Done" in text and "status_key: done" in text
    assert "notion_id: c1" in text
    assert "- **Source**: system-analyst" in text
    assert "corpo de c1" in text
    assert "## Comentários" in text and "entregue" in text


def test_comments_can_be_turned_off(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    client.comments = {"c1": [{"created_time": "x", "rich_text": [{"plain_text": "oi"}]}]}
    sync_mod.sync_kanban(client, board, with_comments=False)
    text = (tmp_path / "knowledge_base" / "notion" / "kanban" / "done" / "primeiro.md").read_text()
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


def test_moving_a_card_moves_its_file_and_leaves_no_duplicate(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Backlog")])
    sync_mod.sync_kanban(client, board)
    assert _tree(tmp_path) == {"backlog": ["primeiro.md"]}

    client.cards = [_card("c1", "Primeiro", "Done", last_edited="2026-08-14T00:00:00.000Z")]
    result = sync_mod.sync_kanban(client, board)
    assert result["moved"] == 1
    assert _tree(tmp_path) == {"done": ["primeiro.md"]}  # backlog/ pruned, not left empty


def test_a_card_gone_from_the_board_is_removed_from_the_mirror(tmp_path, monkeypatch):
    client, board = _kanban_setup(tmp_path, monkeypatch, [
        _card("c1", "Primeiro", "Done"), _card("c2", "Segundo", "Done")])
    sync_mod.sync_kanban(client, board)

    client.cards = [c for c in client.cards if c["id"] == "c2"]
    result = sync_mod.sync_kanban(client, board)
    assert result["removed"] == 1
    assert _tree(tmp_path) == {"done": ["segundo.md"]}


def test_the_kanban_mirror_never_writes_back_to_notion(tmp_path, monkeypatch):
    """Deleting a file under kanban/ must not archive the card — unlike
    notes/, this tree is derived. The file simply comes back."""
    client, board = _kanban_setup(tmp_path, monkeypatch, [_card("c1", "Primeiro", "Done")])
    sync_mod.sync_kanban(client, board)
    (tmp_path / "knowledge_base" / "notion" / "kanban" / "done" / "primeiro.md").unlink()

    result = sync_mod.sync_kanban(client, board)
    assert client.patched == []          # nothing archived
    assert result["added"] == 1          # just re-exported
    assert _tree(tmp_path) == {"done": ["primeiro.md"]}


def test_missing_database_id_is_a_503(tmp_path, monkeypatch):
    client, _ = _kanban_setup(tmp_path, monkeypatch, [])
    try:
        sync_mod.sync_kanban(client, FakeBoard(STATUSES, database_id=""))
    except NotionError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("expected NotionError")


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
