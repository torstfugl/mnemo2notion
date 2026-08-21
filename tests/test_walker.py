"""
End-to-end conversion against a stubbed Notion API.

This is the test that covers the decisions the walker makes rather than the ones
the block converter makes: which folder a page lands in, what becomes a sub-note
versus a folder, and what happens to a database row's properties.
"""

from __future__ import annotations

import unittest

from notion2mnemo import mnemo
from notion2mnemo.assets import AssetStore
from notion2mnemo.colors import ColorMap
from notion2mnemo.package import read_package, write_package
from notion2mnemo.walker import WalkOptions, Walker, normalize_id, page_title

ANNOTATIONS = {
    "bold": False, "italic": False, "strikethrough": False,
    "underline": False, "code": False, "color": "default",
}


def rt(content: str):
    return [{
        "type": "text",
        "text": {"content": content, "link": None},
        "annotations": dict(ANNOTATIONS),
        "plain_text": content,
        "href": None,
    }]


def page(page_id: str, title: str, parent: dict, **extra) -> dict:
    node = {
        "object": "page",
        "id": page_id,
        "parent": parent,
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": "2026-02-01T00:00:00.000Z",
        "properties": {"title": {"type": "title", "title": rt(title)}},
    }
    node.update(extra)
    return node


def para(block_id: str, text: str) -> dict:
    return {
        "id": block_id,
        "type": "paragraph",
        "paragraph": {"rich_text": rt(text), "color": "default"},
        "has_children": False,
    }


class FakeClient:
    """Only the four methods the walker calls."""

    def __init__(self, pages=(), databases=(), children=None, rows=None):
        self._pages = {p["id"]: p for p in pages}
        self._databases = {d["id"]: d for d in databases}
        self._children = children or {}
        self._rows = rows or {}
        self.request_count = 0
        self.cache_hits = 0

    def search_pages(self):
        return list(self._pages.values())

    def search_databases(self):
        return list(self._databases.values())

    def get_page(self, page_id):
        return self._pages[page_id]

    def get_database(self, database_id):
        return self._databases[database_id]

    def block_children(self, block_id):
        return self._children.get(block_id, [])

    def query_database(self, database_id):
        return self._rows.get(database_id, [])

    def download(self, url):
        return b"\x89PNG\r\n\x1a\n", "image/png"


def run(client, options=None):
    assets = AssetStore(downloader=client.download)
    walker = Walker(client, ColorMap(), assets, options or WalkOptions())
    walker.discover()
    return walker.convert(), assets


class Titles(unittest.TestCase):
    def test_database_row_title_is_found_by_property_type_not_name(self):
        # The title property's *name* is the user's choice ("Task", "Name",
        # anything); only its type is fixed.
        row = {"properties": {"Task": {"type": "title", "title": rt("Ship it")}}}
        self.assertEqual(page_title(row), "Ship it")

    def test_a_page_with_no_title_is_untitled(self):
        self.assertEqual(page_title({"properties": {}}), "Untitled")


class Ids(unittest.TestCase):
    def test_a_notion_url_normalises_to_a_dashed_id(self):
        url = "https://www.notion.so/My-Page-1234abcd1234abcd1234abcd1234abcd?pvs=4"
        self.assertEqual(normalize_id(url), "1234abcd-1234-abcd-1234-abcd1234abcd")

    def test_a_bare_id_normalises_too(self):
        self.assertEqual(
            normalize_id("1234abcd1234abcd1234abcd1234abcd"),
            "1234abcd-1234-abcd-1234-abcd1234abcd",
        )

    def test_an_already_dashed_id_is_unchanged(self):
        dashed = "1234abcd-1234-abcd-1234-abcd1234abcd"
        self.assertEqual(normalize_id(dashed), dashed)


class Hierarchy(unittest.TestCase):
    def test_top_level_pages_land_in_the_root_folder(self):
        client = FakeClient(
            pages=[page("p1", "Notes", {"type": "workspace", "workspace": True})],
            children={"p1": [para("b1", "hello")]},
        )
        result, _ = run(client)
        self.assertEqual(len(result.notes), 1)
        note = result.notes[0]
        self.assertEqual(note.title, "Notes")
        self.assertEqual(note.folder_path, "Notion")
        self.assertIsNone(note.parent_note_id)

    def test_a_sub_page_becomes_a_sub_note_not_a_folder(self):
        # Mnemo already models this relationship: ParentNoteId plus a Page block
        # in the parent. Turning it into a folder would move the content out of
        # the page it was written inside.
        client = FakeClient(
            pages=[
                page("p1", "Parent", {"type": "workspace", "workspace": True}),
                page("p2", "Child", {"type": "page_id", "page_id": "p1"}),
            ],
            children={
                "p1": [{"id": "p2", "type": "child_page", "child_page": {"title": "Child"},
                        "has_children": True}],
                "p2": [para("b1", "inner")],
            },
        )
        result, _ = run(client)
        by_title = {n.title: n for n in result.notes}
        self.assertEqual(by_title["Child"].parent_note_id, by_title["Parent"].note_id)
        # Same folder: nesting is by note, not by folder.
        self.assertEqual(by_title["Child"].folder_id, by_title["Parent"].folder_id)
        # And the parent embeds it in place.
        page_blocks = [b for b in by_title["Parent"].blocks if b.type == mnemo.PAGE]
        self.assertEqual(len(page_blocks), 1)
        self.assertEqual(page_blocks[0].payload["referenceNoteId"], by_title["Child"].note_id)

    def test_root_folder_can_be_switched_off(self):
        client = FakeClient(
            pages=[page("p1", "Notes", {"type": "workspace", "workspace": True})],
            children={"p1": []},
        )
        result, _ = run(client, WalkOptions(root_folder=""))
        self.assertEqual(result.folders, [])
        self.assertIsNone(result.notes[0].folder_id)

    def test_an_empty_page_still_gets_one_editable_block(self):
        client = FakeClient(
            pages=[page("p1", "Blank", {"type": "workspace", "workspace": True})],
            children={"p1": []},
        )
        result, _ = run(client)
        self.assertEqual([b.type for b in result.notes[0].blocks], [mnemo.TEXT])

    def test_limit_does_not_leave_a_page_block_pointing_at_nothing(self):
        # A Page block reads its title from the note it references, so a
        # reference to a page --limit excluded would render as nothing.
        client = FakeClient(
            pages=[
                page("p1", "Parent", {"type": "workspace", "workspace": True}),
                page("p2", "Child", {"type": "page_id", "page_id": "p1"}),
            ],
            children={
                "p1": [{"id": "p2", "type": "child_page", "child_page": {"title": "Child"},
                        "has_children": True}],
                "p2": [para("b1", "inner")],
            },
        )
        result, _ = run(client, WalkOptions(limit=1))
        self.assertEqual(len(result.notes), 1)
        written = {n.note_id for n in result.notes}
        for block in result.notes[0].blocks:
            if block.type == mnemo.PAGE:
                self.assertIn(block.payload["referenceNoteId"], written)
        # It degrades to a heading carrying the title instead.
        self.assertTrue(any(b.type == mnemo.HEADING3 for b in result.notes[0].blocks))

    def test_trashed_pages_are_skipped(self):
        client = FakeClient(
            pages=[
                page("p1", "Kept", {"type": "workspace", "workspace": True}),
                page("p2", "Gone", {"type": "workspace", "workspace": True}, in_trash=True),
            ],
            children={"p1": [], "p2": []},
        )
        result, _ = run(client)
        self.assertEqual([n.title for n in result.notes], ["Kept"])


class Databases(unittest.TestCase):
    def build(self, properties: dict, db_properties="table"):
        row = page("r1", "Row one", {"type": "database_id", "database_id": "d1"})
        row["properties"].update(properties)
        client = FakeClient(
            pages=[row],
            databases=[{"object": "database", "id": "d1", "title": rt("Reading list")}],
            children={"r1": [para("b1", "body")]},
            rows={"d1": [row]},
        )
        return run(client, WalkOptions(database_properties=db_properties))

    def test_a_database_becomes_a_folder_of_notes(self):
        result, _ = self.build({})
        folder_names = {f.name for f in result.folders}
        self.assertIn("Reading list", folder_names)
        note = result.notes[0]
        folder = next(f for f in result.folders if f.name == "Reading list")
        self.assertEqual(note.folder_id, folder.folder_id)
        self.assertEqual(note.folder_path, "Notion / Reading list")

    def test_select_and_multi_select_become_tags(self):
        result, _ = self.build({
            "Status": {"type": "status", "status": {"name": "Reading"}},
            "Topics": {"type": "multi_select", "multi_select": [
                {"name": "physics"}, {"name": "optics"}]},
        })
        self.assertEqual(result.notes[0].tags, ["Reading", "optics", "physics"])

    def test_properties_render_as_a_table_at_the_top_of_the_note(self):
        result, _ = self.build({
            "Author": {"type": "rich_text", "rich_text": rt("Feynman")},
            "Pages": {"type": "number", "number": 320},
            "Done": {"type": "checkbox", "checkbox": True},
        })
        first = result.notes[0].blocks[0]
        self.assertEqual(first.type, mnemo.TABLE)
        # The first column holds the property names, which is Mnemo's
        # headerColumns[0].
        self.assertEqual(first.payload["headerColumns"][0], True)
        rows = {
            r.children[0].spans[0].text: r.children[1].spans[0].text for r in first.children
        }
        self.assertEqual(rows["Author"], "Feynman")
        self.assertEqual(rows["Pages"], "320")
        self.assertEqual(rows["Done"], "✓")

    def test_unset_properties_are_left_out_of_the_table(self):
        result, _ = self.build({
            "Author": {"type": "rich_text", "rich_text": []},
            "Pages": {"type": "number", "number": 7},
        })
        first = result.notes[0].blocks[0]
        names = [r.children[0].spans[0].text for r in first.children]
        self.assertEqual(names, ["Pages"])

    def test_properties_can_be_dropped_entirely(self):
        result, _ = self.build({"Pages": {"type": "number", "number": 7}}, db_properties="none")
        self.assertNotEqual(result.notes[0].blocks[0].type, mnemo.TABLE)


class Metadata(unittest.TestCase):
    def test_icon_emoji_and_timestamps_carry_over(self):
        client = FakeClient(
            pages=[page("p1", "Physics", {"type": "workspace", "workspace": True},
                        icon={"type": "emoji", "emoji": "⚛️"})],
            children={"p1": []},
        )
        result, _ = run(client)
        note = result.notes[0]
        self.assertEqual(note.emoji, "⚛️")
        self.assertEqual(note.created_at.year, 2026)
        self.assertEqual(note.modified_at.month, 2)

    def test_cover_is_only_fetched_when_asked_for(self):
        cover = {"type": "external", "external": {"url": "https://x/cover.png"}}
        client = FakeClient(
            pages=[page("p1", "P", {"type": "workspace", "workspace": True}, cover=cover)],
            children={"p1": []},
        )
        without, _ = run(client, WalkOptions(covers=False))
        self.assertIsNone(without.notes[0].cover)

        with_covers, assets = run(client, WalkOptions(covers=True))
        self.assertTrue(with_covers.notes[0].cover.startswith("asset:"))
        self.assertEqual(len(assets.files), 1)


class Idempotency(unittest.TestCase):
    def test_two_conversions_produce_the_same_package(self):
        client = FakeClient(
            pages=[page("p1", "Notes", {"type": "workspace", "workspace": True})],
            children={"p1": [para("b1", "hello")]},
        )
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path

        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in ("a.mnemo", "b.mnemo"):
                result, assets = run(client)
                path = Path(tmp) / name
                write_package(
                    path, result.notes, result.folders, assets.files,
                    app_version="test", created_at=stamp,
                )
                paths.append(path)
            self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
            _, notes, folders, _ = read_package(paths[0])
            self.assertEqual(len(notes), 1)
            self.assertEqual(len(folders), 1)


if __name__ == "__main__":
    unittest.main()
