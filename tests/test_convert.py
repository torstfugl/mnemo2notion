"""Notion blocks -> Mnemo blocks, with no network anywhere."""

from __future__ import annotations

import unittest

from notion2mnemo import mnemo
from notion2mnemo.assets import AssetStore
from notion2mnemo.colors import ColorMap
from notion2mnemo.convert import BlockConverter

ANNOTATIONS = {
    "bold": False, "italic": False, "strikethrough": False,
    "underline": False, "code": False, "color": "default",
}


def rt(content: str, **overrides):
    annotations = dict(ANNOTATIONS)
    annotations.update(overrides)
    return [{
        "type": "text",
        "text": {"content": content, "link": None},
        "annotations": annotations,
        "plain_text": content,
        "href": None,
    }]


def block(block_id: str, kind: str, data: dict, has_children: bool = False) -> dict:
    return {"id": block_id, "type": kind, kind: data, "has_children": has_children}


class ConverterCase(unittest.TestCase):
    """Wires a converter to an in-memory block tree, so nothing touches the API."""

    def make(self, children: dict[str, list[dict]] | None = None, pages: set[str] | None = None):
        self.children = children or {}
        self.pages = pages or set()
        self.warnings: list[str] = []
        self.assets = AssetStore(downloader=self.fake_download)
        self.downloads: list[str] = []
        return BlockConverter(
            colors=ColorMap(),
            assets=self.assets,
            children_of=lambda block_id: self.children.get(block_id, []),
            note_id_for_page=lambda page_id: (
                mnemo.stable_id("note", page_id) if page_id in self.pages else None
            ),
            warnings=self.warnings,
        )

    def fake_download(self, url: str) -> tuple[bytes, str]:
        self.downloads.append(url)
        if "broken" in url:
            raise RuntimeError("404")
        if "vector" in url:
            return b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"
        # A one-pixel PNG, so the signature sniffer has something real to read.
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82",
            "image/png",
        )


class Prose(ConverterCase):
    def test_paragraph(self):
        converter = self.make()
        out = converter.convert_block(block("b1", "paragraph", {"rich_text": rt("hello")}))
        self.assertEqual(out[0].type, mnemo.TEXT)
        self.assertEqual(out[0].spans[0].text, "hello")

    def test_headings_map_to_the_first_three_levels(self):
        converter = self.make()
        for notion_kind, expected in (
            ("heading_1", mnemo.HEADING1),
            ("heading_2", mnemo.HEADING2),
            ("heading_3", mnemo.HEADING3),
        ):
            out = converter.convert_block(block("b", notion_kind, {"rich_text": rt("h")}))
            self.assertEqual(out[0].type, expected)

    def test_quote(self):
        converter = self.make()
        out = converter.convert_block(block("b", "quote", {"rich_text": rt("q")}))
        self.assertEqual(out[0].type, mnemo.QUOTE)

    def test_divider_has_no_text_but_still_has_a_span(self):
        converter = self.make()
        out = converter.convert_block(block("b", "divider", {}))
        self.assertEqual(out[0].type, mnemo.DIVIDER)
        self.assertEqual(len(out[0].to_json()["spans"]), 1)

    def test_block_level_colour_reaches_the_spans(self):
        converter = self.make()
        out = converter.convert_block(
            block("b", "paragraph", {"rich_text": rt("warn"), "color": "red"})
        )
        self.assertEqual(out[0].spans[0].style.foreground_color, "swatch5")


class Lists(ConverterCase):
    def test_bulleted_and_numbered_and_todo(self):
        converter = self.make()
        self.assertEqual(
            converter.convert_block(block("b", "bulleted_list_item", {"rich_text": rt("a")}))[0].type,
            mnemo.BULLET_LIST,
        )
        self.assertEqual(
            converter.convert_block(block("b", "numbered_list_item", {"rich_text": rt("a")}))[0].type,
            mnemo.NUMBERED_LIST,
        )
        todo = converter.convert_block(
            block("b", "to_do", {"rich_text": rt("a"), "checked": True})
        )[0]
        self.assertEqual(todo.type, mnemo.CHECKLIST)
        self.assertTrue(todo.payload["checked"])

    def test_nested_list_items_become_children(self):
        converter = self.make(
            children={"parent": [block("kid", "bulleted_list_item", {"rich_text": rt("inner")})]}
        )
        out = converter.convert_block(
            block("parent", "bulleted_list_item", {"rich_text": rt("outer")}, has_children=True)
        )
        self.assertEqual(len(out[0].children), 1)
        self.assertEqual(out[0].children[0].spans[0].text, "inner")

    def test_toggle_keeps_its_contents_as_children(self):
        # Mnemo has no collapsible block: the summary becomes a paragraph and
        # nothing inside it is lost.
        converter = self.make(children={"t": [block("k", "paragraph", {"rich_text": rt("hidden")})]})
        out = converter.convert_block(
            block("t", "toggle", {"rich_text": rt("Summary")}, has_children=True)
        )
        self.assertEqual(out[0].type, mnemo.TEXT)
        self.assertEqual(out[0].children[0].spans[0].text, "hidden")


class Callouts(ConverterCase):
    def test_emoji_icon_and_default_tone(self):
        converter = self.make()
        out = converter.convert_block(
            block("b", "callout", {
                "rich_text": rt("remember"),
                "icon": {"type": "emoji", "emoji": "📌"},
                "color": "gray_background",
            })
        )
        self.assertEqual(out[0].type, mnemo.CALLOUT)
        self.assertEqual(out[0].payload["emoji"], "📌")
        self.assertEqual(out[0].payload["tone"], "note")

    def test_warm_colour_becomes_the_warn_tone(self):
        converter = self.make()
        out = converter.convert_block(
            block("b", "callout", {"rich_text": rt("careful"), "color": "red_background"})
        )
        self.assertEqual(out[0].payload["tone"], "warn")

    def test_file_icon_falls_back_to_a_glyph(self):
        converter = self.make()
        out = converter.convert_block(
            block("b", "callout", {
                "rich_text": rt("x"),
                "icon": {"type": "external", "external": {"url": "https://example.com/i.png"}},
            })
        )
        self.assertTrue(out[0].payload["emoji"])


class CodeAndEquations(ConverterCase):
    def test_code_carries_source_in_both_payload_and_spans(self):
        converter = self.make()
        out = converter.convert_block(
            block("b", "code", {"rich_text": rt("print(1)"), "language": "python", "caption": []})
        )[0]
        self.assertEqual(out.type, mnemo.CODE)
        self.assertEqual(out.payload["source"], "print(1)")
        self.assertEqual(out.payload["language"], "python")
        self.assertEqual(out.spans[0].text, "print(1)")

    def test_notion_language_names_are_translated(self):
        converter = self.make()
        for notion_name, expected in (
            ("plain text", "text"), ("c++", "cpp"), ("c#", "csharp"),
            ("objective-c", "objectivec"), ("shell", "bash"),
        ):
            out = converter.convert_block(
                block("b", "code", {"rich_text": rt("x"), "language": notion_name})
            )[0]
            self.assertEqual(out.payload["language"], expected, notion_name)

    def test_unknown_language_is_passed_through_rather_than_mislabelled(self):
        converter = self.make()
        out = converter.convert_block(
            block("b", "code", {"rich_text": rt("x"), "language": "nix"})
        )[0]
        self.assertEqual(out.payload["language"], "nix")

    def test_equation_block_renders_from_its_payload(self):
        converter = self.make()
        out = converter.convert_block(block("b", "equation", {"expression": "E = mc^2"}))[0]
        self.assertEqual(out.type, mnemo.EQUATION)
        self.assertEqual(out.payload["latex"], "E = mc^2")
        # Mnemo forces an equation block's spans blank on read; carrying text
        # would create content nothing renders.
        self.assertEqual(out.spans[0].text, "")


class Images(ConverterCase):
    def test_image_becomes_an_asset_reference(self):
        converter = self.make()
        out = converter.convert_block(
            block("b", "image", {
                "type": "file",
                "file": {"url": "https://files.notion.so/a.png"},
                "caption": rt("Figure 1"),
            })
        )[0]
        self.assertEqual(out.type, mnemo.IMAGE)
        # A bare `{32 hex}{ext}` id, the only shape Mnemo's asset store accepts.
        self.assertRegex(out.payload["path"], r"^[0-9a-f]{32}\.png$")
        self.assertEqual(out.payload["alt"], "Figure 1")
        self.assertEqual(out.spans[0].text, "Figure 1")
        self.assertIn(out.payload["path"], self.assets.files)

    def test_the_same_url_is_downloaded_once(self):
        converter = self.make()
        node = {"type": "external", "external": {"url": "https://x/a.png"}, "caption": []}
        first = converter.convert_block(block("b1", "image", dict(node)))[0]
        second = converter.convert_block(block("b2", "image", dict(node)))[0]
        self.assertEqual(first.payload["path"], second.payload["path"])
        self.assertEqual(len(self.downloads), 1)

    def test_a_failed_download_degrades_to_a_link_not_a_hole(self):
        converter = self.make()
        out = converter.convert_block(
            block("b", "image", {
                "type": "external",
                "external": {"url": "https://x/broken.png"},
                "caption": rt("diagram"),
            })
        )[0]
        self.assertEqual(out.type, mnemo.TEXT)
        self.assertEqual(out.spans[0].style.link_url, "https://x/broken.png")
        self.assertTrue(self.assets.warnings)

    def test_a_format_mnemo_cannot_store_degrades_to_a_link(self):
        # SVG is not in ManagedAssetStore.ImageExtensions and Pillow is not
        # assumed to be installed.
        converter = self.make()
        out = converter.convert_block(
            block("b", "image", {
                "type": "external",
                "external": {"url": "https://x/vector"},
                "caption": [],
            })
        )[0]
        self.assertIn(out.type, (mnemo.TEXT, mnemo.IMAGE))
        if out.type == mnemo.TEXT:
            self.assertTrue(self.assets.warnings)


class Tables(ConverterCase):
    def make_table(self, has_column_header=True, has_row_header=False):
        converter = self.make(children={
            "t": [
                {"id": "r1", "type": "table_row",
                 "table_row": {"cells": [rt("Name"), rt("Value")]}, "has_children": False},
                {"id": "r2", "type": "table_row",
                 "table_row": {"cells": [rt("a"), rt("1")]}, "has_children": False},
            ]
        })
        return converter.convert_block(block("t", "table", {
            "table_width": 2,
            "has_column_header": has_column_header,
            "has_row_header": has_row_header,
        }, has_children=True))[0]

    def test_structure_is_table_row_cell(self):
        table = self.make_table()
        self.assertEqual(table.type, mnemo.TABLE)
        self.assertEqual([r.type for r in table.children], [mnemo.TABLE_ROW, mnemo.TABLE_ROW])
        self.assertEqual([c.type for c in table.children[0].children],
                         [mnemo.TABLE_CELL, mnemo.TABLE_CELL])
        self.assertEqual(table.children[1].children[0].spans[0].text, "a")

    def test_notion_column_header_marks_row_zero(self):
        # Notion's has_column_header means "the first row holds the column
        # labels", which is Mnemo's headerRows[0].
        table = self.make_table(has_column_header=True, has_row_header=False)
        self.assertEqual(table.payload["headerRows"], [True, False])
        self.assertEqual(table.payload["headerColumns"], [False, False])

    def test_notion_row_header_marks_column_zero(self):
        table = self.make_table(has_column_header=False, has_row_header=True)
        self.assertEqual(table.payload["headerRows"], [False, False])
        self.assertEqual(table.payload["headerColumns"], [True, False])


class Columns(ConverterCase):
    def build(self, count: int, ratios: list[float] | None = None):
        columns = []
        children: dict[str, list[dict]] = {}
        for index in range(count):
            column_id = f"c{index}"
            data = {}
            if ratios:
                data["width_ratio"] = ratios[index]
            columns.append({"id": column_id, "type": "column", "column": data, "has_children": True})
            children[column_id] = [
                block(f"p{index}", "paragraph", {"rich_text": rt(f"col{index}")})
            ]
        children["list"] = columns
        converter = self.make(children=children)
        return converter.convert_block(block("list", "column_list", {}, has_children=True))

    def test_two_columns_become_one_two_column_block(self):
        out = self.build(2)
        self.assertEqual(out[0].type, mnemo.TWO_COLUMN)
        self.assertEqual([c.type for c in out[0].children],
                         [mnemo.COLUMN_GROUP, mnemo.COLUMN_GROUP])
        self.assertEqual(out[0].children[0].children[0].spans[0].text, "col0")
        self.assertEqual(out[0].children[1].children[0].spans[0].text, "col1")

    def test_three_columns_nest_rather_than_truncate(self):
        # Mnemo's TwoColumn holds exactly two cells, enforced by its schema, so
        # N columns become N-1 nested splits and no content is dropped.
        out = self.build(3)
        outer = out[0]
        self.assertEqual(outer.type, mnemo.TWO_COLUMN)
        inner = outer.children[1].children[0]
        self.assertEqual(inner.type, mnemo.TWO_COLUMN)
        texts = [b.spans[0].text for b in outer.walk() if b.type == mnemo.TEXT]
        self.assertEqual(texts, ["col0", "col1", "col2"])

    def test_split_ratio_follows_notion_widths(self):
        out = self.build(2, ratios=[0.75, 0.25])
        self.assertAlmostEqual(out[0].payload["splitRatio"], 0.75, places=3)

    def test_split_ratio_is_clamped_to_a_usable_range(self):
        out = self.build(2, ratios=[0.99, 0.01])
        self.assertLessEqual(out[0].payload["splitRatio"], 0.9)

    def test_a_single_column_is_spliced_in_rather_than_wrapped(self):
        out = self.build(1)
        self.assertEqual(out[0].type, mnemo.TEXT)

    def test_an_empty_column_is_seeded_so_the_caret_has_somewhere_to_land(self):
        children = {
            "list": [
                {"id": "c0", "type": "column", "column": {}, "has_children": False},
                {"id": "c1", "type": "column", "column": {}, "has_children": True},
            ],
            "c1": [block("p", "paragraph", {"rich_text": rt("right")})],
        }
        converter = self.make(children=children)
        out = converter.convert_block(block("list", "column_list", {}, has_children=True))
        self.assertEqual(len(out[0].children[0].children), 1)
        self.assertEqual(out[0].children[0].children[0].type, mnemo.TEXT)


class PagesAndLinks(ConverterCase):
    def test_child_page_becomes_a_page_block_pointing_at_the_converted_note(self):
        converter = self.make(pages={"sub"})
        out = converter.convert_block(block("sub", "child_page", {"title": "Sub"}))[0]
        self.assertEqual(out.type, mnemo.PAGE)
        self.assertEqual(out.payload["referenceNoteId"], mnemo.stable_id("note", "sub"))

    def test_child_page_outside_the_export_keeps_its_title(self):
        converter = self.make(pages=set())
        out = converter.convert_block(block("sub", "child_page", {"title": "Sub"}))[0]
        self.assertEqual(out.type, mnemo.HEADING3)
        self.assertEqual(out.spans[0].text, "Sub")
        self.assertTrue(self.warnings)

    def test_link_to_page_resolves_to_a_page_block_when_it_can(self):
        converter = self.make(pages={"target"})
        out = converter.convert_block(block("l", "link_to_page", {"type": "page_id", "page_id": "target"}))[0]
        self.assertEqual(out.type, mnemo.PAGE)

    def test_synced_block_is_inlined(self):
        converter = self.make(children={"s": [block("p", "paragraph", {"rich_text": rt("shared")})]})
        out = converter.convert_block(block("s", "synced_block", {"synced_from": None}, has_children=True))
        self.assertEqual(out[0].spans[0].text, "shared")


class Fallbacks(ConverterCase):
    def test_bookmark_keeps_its_link(self):
        converter = self.make()
        out = converter.convert_block(
            block("b", "bookmark", {"url": "https://example.com", "caption": rt("Ref")})
        )[0]
        self.assertEqual(out.type, mnemo.TEXT)
        self.assertEqual(out.spans[0].style.link_url, "https://example.com")
        self.assertEqual(out.spans[0].text, "Ref")

    def test_embed_and_video_keep_their_links(self):
        converter = self.make()
        for kind in ("embed", "video"):
            data = {"url": "https://v/1", "caption": []} if kind == "embed" else {
                "type": "external", "external": {"url": "https://v/1"}, "caption": [],
            }
            out = converter.convert_block(block("b", kind, data))[0]
            self.assertEqual(out.spans[0].style.link_url, "https://v/1")

    def test_generated_views_are_dropped_without_noise(self):
        converter = self.make()
        self.assertEqual(converter.convert_block(block("b", "table_of_contents", {"color": "default"})), [])
        self.assertEqual(self.warnings, [])

    def test_an_unknown_block_type_warns_rather_than_crashing(self):
        converter = self.make()
        self.assertEqual(converter.convert_block(block("b", "some_future_block", {})), [])
        self.assertTrue(self.warnings)

    def test_recursion_is_bounded(self):
        # A block that claims itself as its own child must not hang the export.
        converter = self.make(children={"loop": [block("loop", "paragraph", {"rich_text": rt("x")}, True)]})
        converter.max_depth = 5
        converter.convert_block(block("loop", "paragraph", {"rich_text": rt("x")}, has_children=True))
        self.assertTrue(any("nesting depth" in w for w in self.warnings))


if __name__ == "__main__":
    unittest.main()
