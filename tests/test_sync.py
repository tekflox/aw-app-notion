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
    d = tmp_path / "knowledge_base" / "notes"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# ── paths ───────────────────────────────────────────────────────────────

def test_notes_land_in_the_kb_tree_not_the_app_dir(tmp_path, monkeypatch):
    """The monolith wrote into its own repo. Here the KB is a separate
    container whose indexed tree is <workspace_home>/knowledge_base — writing
    anywhere else produces notes nothing ever indexes."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", "/srv/home")
    assert sync_mod.notes_dir() == "/srv/home/knowledge_base/notes"
    assert sync_mod.state_path() == "/srv/home/data/notion/notion_sync_state.json"


# ── pull ────────────────────────────────────────────────────────────────

def test_sync_writes_a_note_per_child_page(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    result = sync_mod.run_sync(client, ROOT_PAGE)
    assert result["added"] == 2
    assert result["updated"] == result["skipped"] == 0
    assert _notes(tmp_path) == ["nota-dois.md", "nota-um.md"]


def test_note_carries_the_monoliths_frontmatter(tmp_path, monkeypatch):
    """Keeping source/repo/path/notion_id/checksum/last_edited identical is
    what lets an already-indexed note keep its identity across the move."""
    client = _setup(tmp_path, monkeypatch, titles=("Nota Um",))
    sync_mod.run_sync(client, ROOT_PAGE)
    text = (tmp_path / "knowledge_base" / "notes" / "nota-um.md").read_text()
    assert text.startswith("---\nsource: notion\nrepo: notes\npath: notes/nota-um.md\n")
    assert "notion_id: page-0" in text
    assert "checksum: " in text
    assert "# Nota Um" in text
    assert "conteúdo de Nota Um" in text


def test_unchanged_pages_are_skipped_on_a_second_run(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.run_sync(client, ROOT_PAGE)
    second = sync_mod.run_sync(client, ROOT_PAGE)
    assert second["skipped"] == 2
    assert second["added"] == second["updated"] == 0


def test_force_rewrites_everything(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.run_sync(client, ROOT_PAGE)
    forced = sync_mod.run_sync(client, ROOT_PAGE, force=True)
    assert forced["added"] == 2
    assert forced["skipped"] == 0


def test_an_edited_page_counts_as_updated(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, titles=("Nota Um",))
    sync_mod.run_sync(client, ROOT_PAGE)
    client.children["page-0"] = [_para("texto novo")]
    result = sync_mod.run_sync(client, ROOT_PAGE)
    assert result["updated"] == 1
    assert "texto novo" in (tmp_path / "knowledge_base" / "notes" / "nota-um.md").read_text()


def test_one_unreadable_page_does_not_abort_the_others(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    del client.pages["page-0"]  # 404s on fetch
    result = sync_mod.run_sync(client, ROOT_PAGE)
    assert result["added"] == 1
    assert any("failed to fetch" in line for line in result["log"])


def test_state_records_every_synced_page(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.run_sync(client, ROOT_PAGE)
    state = json.loads(Path(sync_mod.state_path()).read_text())
    assert set(state["pages"]) == {"page-0", "page-1"}
    assert state["pages"]["page-0"]["slug"] == "nota-um"
    assert state["last_sync"]


def test_missing_root_page_id_is_a_503_not_a_crash(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    try:
        sync_mod.run_sync(client, "")
    except NotionError as exc:
        assert exc.status == 503
        assert "sync_root_page_id" in str(exc)
    else:
        raise AssertionError("expected NotionError")


# ── push (bidirectional) ────────────────────────────────────────────────

def test_pull_only_never_archives_anything(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.run_sync(client, ROOT_PAGE)
    (tmp_path / "knowledge_base" / "notes" / "nota-um.md").unlink()
    result = sync_mod.run_sync(client, ROOT_PAGE, bidirectional=False)
    assert result["deleted"] == 0
    assert client.patched == []


def test_deleting_a_note_archives_its_notion_page(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.run_sync(client, ROOT_PAGE, bidirectional=True)
    (tmp_path / "knowledge_base" / "notes" / "nota-um.md").unlink()

    # the page is gone from Notion's side too once archived
    client.children[ROOT_PAGE] = [b for b in client.children[ROOT_PAGE] if b["id"] != "page-0"]
    result = sync_mod.run_sync(client, ROOT_PAGE, bidirectional=True)
    assert client.patched == [("page-0", {"archived": True})]
    assert result["deleted"] == 1
    state = json.loads(Path(sync_mod.state_path()).read_text())
    assert "page-0" not in state["pages"]


def test_a_page_archived_in_notion_removes_the_local_note(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    sync_mod.run_sync(client, ROOT_PAGE, bidirectional=True)
    client.children[ROOT_PAGE] = [b for b in client.children[ROOT_PAGE] if b["id"] != "page-1"]

    result = sync_mod.run_sync(client, ROOT_PAGE, bidirectional=True)
    assert result["deleted"] == 1
    assert _notes(tmp_path) == ["nota-um.md"]


# ── KB rebuild ──────────────────────────────────────────────────────────

def test_rebuild_is_skipped_when_nothing_changed(tmp_path, monkeypatch):
    calls = []
    client = _setup(tmp_path, monkeypatch)
    sync_mod.run_sync(client, ROOT_PAGE)
    monkeypatch.setattr(sync_mod, "_rebuild_kb", lambda: calls.append(1) or {"ok": True})
    result = sync_mod.run_sync(client, ROOT_PAGE)
    assert calls == []
    assert result["kb_rebuild"] is None


def test_no_rebuild_writes_the_notes_anyway(tmp_path, monkeypatch):
    calls = []
    client = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sync_mod, "_rebuild_kb", lambda: calls.append(1) or {"ok": True})
    result = sync_mod.run_sync(client, ROOT_PAGE, rebuild=False)
    assert calls == []
    assert result["added"] == 2
    assert _notes(tmp_path) == ["nota-dois.md", "nota-um.md"]


def test_a_failed_rebuild_still_reports_the_written_notes(tmp_path, monkeypatch):
    """Best-effort by design: the notes are on disk, the next build picks them
    up — but it must not read as a clean run either."""
    client = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sync_mod, "_rebuild_kb",
                        lambda: {"ok": False, "status": 0, "error": "connection refused"})
    result = sync_mod.run_sync(client, ROOT_PAGE)
    assert result["added"] == 2
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
    sync_mod.run_sync(client, ROOT_PAGE)
    assert "*(página vazia)*" in (tmp_path / "knowledge_base" / "notes" / "vazia.md").read_text()
