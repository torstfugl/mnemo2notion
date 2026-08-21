"""Mnemo blocks -> Notion blocks, and the writer against a fake API."""

from __future__ import annotations

import unittest
from typing import Any

from notion2mnemo.reverse import (
    MAX_TEXT_LENGTH,
    NotionNode,
    ReverseContext,
    block_to_notion,
    note_to_nodes,
    span_to_rich_text,
)


def ctx(resolver=None) -> ReverseContext:
    return ReverseContext(resolve_image=resolver or (lambda _path: None))


def span(text: str, **style) -> dict[str, Any]:
    return {"kind": "text", "text": text, "style": style}


def block(block_type: str, spans=None, payload=None, children=None) -> dict[str, Any]:
    return {
        "type": block_type,
        "spans": spans if spans is not None else [span("x")],
        "payload": payload or {"kind": "empty"},
        "children": children or [],
    }


class RichText(unittest.TestCase):
    def test_flags_map_back_one_to_one(self):
        c = ctx()
        rich = span_to_rich_text(span("a", bold=True, italic=True, underline=True,
                                      strikethrough=True, code=True), c)[0]
        a = rich["annotations"]
        self.assertTrue(all([a["bold"], a["italic"], a["underline"], a["strikethrough"], a["code"]]))

    def test_foreground_token_becomes_a_notion_text_colour(self):
        rich = span_to_rich_text(span("a", foregroundColor="swatch5"), ctx())[0]
        self.assertEqual(rich["annotations"]["color"], "red")

    def test_background_token_becomes_a_notion_background(self):
        rich = span_to_rich_text(span("a", backgroundColor="swatch7"), ctx())[0]
        self.assertEqual(rich["annotations"]["color"], "yellow_background")

    def test_background_wins_when_a_span_carries_both(self):
        # Notion holds one colour per run; the highlight is the salient one.
        rich = span_to_rich_text(
            span("a", foregroundColor="swatch5", backgroundColor="swatch9"), ctx()
        )[0]
        self.assertEqual(rich["annotations"]["color"], "blue_background")

    def test_highlight_flag_becomes_yellow_background(self):
        rich = span_to_rich_text(span("a", highlight=True), ctx())[0]
        self.assertEqual(rich["annotations"]["color"], "yellow_background")

    def test_round_trip_convergence(self):
        # Forward maps notion red -> swatch5; backward maps swatch5 -> red.
        # A note that goes Notion -> Mnemo -> Notion keeps its colour.
        from notion2mnemo.colors import TEXT_COLORS
        from notion2mnemo.reverse import FOREGROUND_TO_NOTION

        for notion_color, token in TEXT_COLORS.items():
            back = FOREGROUND_TO_NOTION[token]
            # Collisions collapse (pink -> red), but a second trip is stable.
            self.assertEqual(FOREGROUND_TO_NOTION[TEXT_COLORS[back]], back, notion_color)

    def test_link_carries_over(self):
        rich = span_to_rich_text(span("Mnemo", linkUrl="https://mnemo.one"), ctx())[0]
        self.assertEqual(rich["text"]["link"]["url"], "https://mnemo.one")

    def test_long_text_splits_at_notions_cap(self):
        rich = span_to_rich_text(span("x" * 4500), ctx())
        self.assertEqual([len(r["text"]["content"]) for r in rich], [2000, 2000, 500])

    def test_inline_equation_becomes_an_equation_run(self):
        rich = span_to_rich_text({"kind": "equation", "latex": "e^x", "style": {}}, ctx())[0]
        self.assertEqual(rich["type"], "equation")
        self.assertEqual(rich["equation"]["expression"], "e^x")

    def test_oversized_equation_degrades_to_code_with_a_warning(self):
        c = ctx()
        rich = span_to_rich_text({"kind": "equation", "latex": "x" * 1500, "style": {}}, c)
        self.assertEqual(rich[0]["type"], "text")
        self.assertTrue(rich[0]["annotations"]["code"])
        self.assertTrue(c.warnings)


class Blocks(unittest.TestCase):
    def test_headings_and_the_fourth_level(self):
        for mnemo_type, notion_type in (
            ("Heading1", "heading_1"), ("Heading2", "heading_2"),
            ("Heading3", "heading_3"), ("Heading4", "heading_3"),
        ):
            node = block_to_notion(block(mnemo_type), ctx())
            self.assertEqual(node.block["type"], notion_type, mnemo_type)

    def test_checklist_keeps_its_state(self):
        node = block_to_notion(block("Checklist", payload={"kind": "checklist", "checked": True}), ctx())
        self.assertTrue(node.block["to_do"]["checked"])

    def test_code_keeps_language_and_caption(self):
        node = block_to_notion(
            block("Code", payload={"kind": "code", "language": "csharp",
                                   "source": "var x = 1;", "caption": "sample"}),
            ctx(),
        )
        self.assertEqual(node.block["code"]["language"], "c#")
        self.assertEqual(node.block["code"]["rich_text"][0]["text"]["content"], "var x = 1;")
        self.assertEqual(node.block["code"]["caption"][0]["text"]["content"], "sample")

    def test_equation_block(self):
        node = block_to_notion(
            block("Equation", spans=[span("")], payload={"kind": "equation", "latex": "E=mc^2"}), ctx()
        )
        self.assertEqual(node.block["equation"]["expression"], "E=mc^2")

    def test_callout_keeps_emoji_and_maps_tone(self):
        node = block_to_notion(
            block("Callout", payload={"kind": "callout", "emoji": "💡", "tone": "warn"}), ctx()
        )
        self.assertEqual(node.block["callout"]["icon"]["emoji"], "💡")
        self.assertEqual(node.block["callout"]["color"], "red_background")

    def test_children_ride_on_the_node_not_in_the_block(self):
        node = block_to_notion(
            block("BulletList", children=[block("BulletList", spans=[span("inner")])]), ctx()
        )
        self.assertEqual(len(node.children), 1)
        self.assertNotIn("children", node.block["bulleted_list_item"])

    def test_image_with_package_asset(self):
        resolver = lambda path: {"type": "file_upload", "file_upload": {"id": "u1"}}
        node = block_to_notion(
            block("Image", spans=[span("caption")],
                  payload={"kind": "image", "path": "a" * 32 + ".png", "alt": "caption"}),
            ctx(resolver),
        )
        self.assertEqual(node.block["image"]["file_upload"]["id"], "u1")
        self.assertEqual(node.block["image"]["caption"][0]["text"]["content"], "caption")

    def test_unresolvable_image_degrades_to_text_with_warning(self):
        c = ctx()
        node = block_to_notion(
            block("Image", spans=[span("diagram")],
                  payload={"kind": "image", "path": "missing.png", "alt": "diagram"}),
            c,
        )
        self.assertEqual(node.block["type"], "paragraph")
        self.assertTrue(c.warnings)


class Tables(unittest.TestCase):
    def make_table(self, header_rows=None, header_columns=None):
        cell = lambda text: block("TableCell", spans=[span(text)])
        row = lambda *texts: block("TableRow", spans=[span("")], children=[cell(t) for t in texts])
        return block(
            "Table",
            spans=[span("")],
            payload={"kind": "table", "columnWidths": [],
                     "headerRows": header_rows or [True, False],
                     "headerColumns": header_columns or [False, False],
                     "fullWidth": False},
            children=[row("Name", "Value"), row("a", "1")],
        )

    def test_rows_are_inline_children(self):
        node = block_to_notion(self.make_table(), ctx())
        self.assertTrue(node.inline_children)
        self.assertEqual(node.block["table"]["table_width"], 2)
        self.assertEqual(len(node.children), 2)
        cells = node.children[0].block["table_row"]["cells"]
        self.assertEqual(cells[0][0]["text"]["content"], "Name")

    def test_header_flags_map_to_notions_firsts(self):
        node = block_to_notion(self.make_table(header_rows=[True, False]), ctx())
        self.assertTrue(node.block["table"]["has_column_header"])
        self.assertFalse(node.block["table"]["has_row_header"])

    def test_non_first_header_warns(self):
        c = ctx()
        block_to_notion(self.make_table(header_rows=[False, True]), c)
        self.assertTrue(any("cannot represent" in w for w in c.warnings))

    def test_ragged_rows_are_padded(self):
        cell = lambda text: block("TableCell", spans=[span(text)])
        table = block(
            "Table", spans=[span("")],
            payload={"kind": "table", "columnWidths": [], "headerRows": [],
                     "headerColumns": [], "fullWidth": False},
            children=[
                block("TableRow", spans=[span("")], children=[cell("a"), cell("b")]),
                block("TableRow", spans=[span("")], children=[cell("only")]),
            ],
        )
        node = block_to_notion(table, ctx())
        self.assertEqual(len(node.children[1].block["table_row"]["cells"]), 2)


class Columns(unittest.TestCase):
    def make_split(self, ratio=0.6):
        group = lambda *blocks: block("ColumnGroup", spans=[span("")], children=list(blocks))
        return block(
            "TwoColumn",
            spans=[span("")],
            payload={"kind": "twoColumn", "splitRatio": ratio},
            children=[group(block("Text", spans=[span("left")])),
                      group(block("Text", spans=[span("right")]))],
        )

    def test_two_columns_with_width_ratios(self):
        node = block_to_notion(self.make_split(0.6), ctx())
        self.assertEqual(node.block["type"], "column_list")
        self.assertTrue(node.inline_children)
        self.assertEqual(len(node.children), 2)
        self.assertAlmostEqual(node.children[0].block["column"]["width_ratio"], 0.6)
        self.assertAlmostEqual(node.children[1].block["column"]["width_ratio"], 0.4)

    def test_empty_column_gets_a_placeholder(self):
        group = lambda *blocks: block("ColumnGroup", spans=[span("")], children=list(blocks))
        split = block(
            "TwoColumn", spans=[span("")],
            payload={"kind": "twoColumn", "splitRatio": 0.5},
            children=[group(), group(block("Text", spans=[span("right")]))],
        )
        node = block_to_notion(split, ctx())
        # Notion rejects an empty column outright, so one paragraph is seeded.
        self.assertEqual(len(node.children[0].children), 1)

    def test_page_block_becomes_a_note_reference(self):
        node = block_to_notion(
            block("Page", spans=[span("")], payload={"kind": "page", "referenceNoteId": "n42"}), ctx()
        )
        self.assertIsNone(node.block)
        self.assertEqual(node.note_ref, "n42")


class FakeWriterClient:
    """Records every write; answers reads with what the writes created."""

    def __init__(self):
        self.pages = []
        self.appends = []
        self.uploads = []
        self._children: dict[str, list[dict]] = {}
        self._counter = 0

    def _new_id(self, prefix):
        self._counter += 1
        return f"{prefix}{self._counter}"

    def create_page(self, parent_page_id, title, *, icon_emoji=None, children=None):
        page_id = self._new_id("page")
        self.pages.append({"id": page_id, "parent": parent_page_id, "title": title, "icon": icon_emoji})
        return {"id": page_id}

    def append_children(self, block_id, children):
        self.appends.append((block_id, children))
        results = []
        for child in children:
            child_id = self._new_id("block")
            created = {"id": child_id, "type": child.get("type")}
            results.append(created)
            self._children.setdefault(block_id, []).append(created)
            # Inline-embedded children become readable, like the real API.
            if child.get("type") == "column_list":
                for column in child["column_list"]["children"]:
                    column_id = self._new_id("col")
                    self._children.setdefault(child_id, []).append({"id": column_id, "type": "column"})
                    for grand in column["column"]["children"]:
                        self._children.setdefault(column_id, []).append(
                            {"id": self._new_id("blk"), "type": grand.get("type")}
                        )
        return results

    def block_children_fresh(self, block_id):
        return list(self._children.get(block_id, []))

    def upload_file(self, filename, content_type, data):
        self.uploads.append(filename)
        return self._new_id("upload")


class Writer(unittest.TestCase):
    def make_package(self, tmp, notes_blocks, title="Note"):
        from pathlib import Path

        from notion2mnemo import mnemo as m
        from notion2mnemo.package import write_package

        note = m.Note(note_id="n1", title=title, emoji="⚛️",
                      blocks=notes_blocks)
        path = Path(tmp) / "t.mnemo"
        write_package(path, [note], [], {"a" * 32 + ".png": b"\x89PNG\r\n\x1a\nx"}, app_version="t")
        return str(path)

    def run_push(self, notes_blocks):
        import tempfile

        from notion2mnemo import mnemo as m
        from notion2mnemo.push import NotionWriter

        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_package(tmp, notes_blocks)
            client = FakeWriterClient()
            writer = NotionWriter(client)  # type: ignore[arg-type]
            result = writer.push_package(path, "root")
            return client, result

    def test_page_created_with_title_and_icon(self):
        from notion2mnemo import mnemo as m

        client, result = self.run_push([m.Block(type=m.TEXT, spans=[m.plain("hello")])])
        self.assertEqual(client.pages[0]["title"], "Note")
        self.assertEqual(client.pages[0]["icon"], "⚛️")
        self.assertEqual(result.pages_created, 1)
        self.assertEqual(result.blocks_written, 1)

    def test_batches_cap_at_one_hundred(self):
        from notion2mnemo import mnemo as m

        blocks = [m.Block(type=m.TEXT, spans=[m.plain(f"p{i}")]) for i in range(250)]
        client, result = self.run_push(blocks)
        sizes = [len(children) for _id, children in client.appends]
        self.assertEqual(sizes, [100, 100, 50])
        self.assertEqual(result.blocks_written, 250)

    def test_nested_children_are_appended_to_the_created_parent(self):
        from notion2mnemo import mnemo as m

        bullet = m.Block(type=m.BULLET_LIST, spans=[m.plain("outer")],
                         children=[m.Block(type=m.BULLET_LIST, spans=[m.plain("inner")])])
        client, _ = self.run_push([bullet])
        # Two appends: the bullet to the page, then its child to the bullet's id.
        self.assertEqual(len(client.appends), 2)
        parent_of_inner = client.appends[1][0]
        self.assertTrue(parent_of_inner.startswith("block"))

    def test_image_is_uploaded_and_attached(self):
        from notion2mnemo import mnemo as m

        image = m.Block(type=m.IMAGE, spans=[m.plain("cap")],
                        payload=m.image_payload("a" * 32 + ".png", alt="cap"))
        client, result = self.run_push([image])
        self.assertEqual(client.uploads, ["a" * 32 + ".png"])
        self.assertEqual(result.images_uploaded, 1)
        appended = client.appends[0][1][0]
        self.assertEqual(appended["type"], "image")
        self.assertEqual(appended["image"]["type"], "file_upload")

    def test_table_rides_in_one_request(self):
        from notion2mnemo import mnemo as m

        cell = lambda t: m.Block(type=m.TABLE_CELL, spans=[m.plain(t)], payload=m.table_cell_payload())
        row = lambda *ts: m.Block(type=m.TABLE_ROW, children=[cell(t) for t in ts])
        table = m.Block(type=m.TABLE,
                        payload=m.table_payload([], [True], [False, False]),
                        children=[row("a", "b"), row("c", "d")])
        client, _ = self.run_push([table])
        appended = client.appends[0][1][0]
        self.assertEqual(appended["type"], "table")
        self.assertEqual(len(appended["table"]["children"]), 2)

    def test_sub_note_is_created_at_its_page_blocks_position(self):
        import tempfile
        from pathlib import Path

        from notion2mnemo import mnemo as m
        from notion2mnemo.package import write_package
        from notion2mnemo.push import NotionWriter

        parent = m.Note(note_id="n1", title="Parent", blocks=[
            m.Block(type=m.TEXT, spans=[m.plain("before")]),
            m.Block(type=m.PAGE, payload=m.page_payload("n2")),
            m.Block(type=m.TEXT, spans=[m.plain("after")]),
        ])
        child = m.Note(note_id="n2", title="Child", parent_note_id="n1",
                       blocks=[m.Block(type=m.TEXT, spans=[m.plain("inner")])])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.mnemo"
            write_package(path, [parent, child], [], {}, app_version="t")
            client = FakeWriterClient()
            result = NotionWriter(client).push_package(str(path), "root")  # type: ignore[arg-type]

        self.assertEqual([p["title"] for p in client.pages], ["Parent", "Child"])
        # The child page's parent is the Parent page, and 'before' was flushed
        # before the child was created - order preserved.
        self.assertEqual(client.pages[1]["parent"], client.pages[0]["id"])
        first_append = client.appends[0]
        self.assertEqual(first_append[1][0]["paragraph"]["rich_text"][0]["text"]["content"], "before")
        self.assertEqual(result.pages_created, 2)

    def test_folders_become_pages(self):
        import tempfile
        from pathlib import Path

        from notion2mnemo import mnemo as m
        from notion2mnemo.package import write_package
        from notion2mnemo.push import NotionWriter

        note = m.Note(note_id="n1", title="Inside", folder_id="f1",
                      blocks=[m.Block(type=m.TEXT, spans=[m.plain("x")])])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.mnemo"
            write_package(path, [note], [m.Folder(folder_id="f1", name="Physics")], {},
                          app_version="t")
            client = FakeWriterClient()
            NotionWriter(client).push_package(str(path), "root")  # type: ignore[arg-type]

        self.assertEqual([p["title"] for p in client.pages], ["Physics", "Inside"])
        self.assertEqual(client.pages[1]["parent"], client.pages[0]["id"])


if __name__ == "__main__":
    unittest.main()
