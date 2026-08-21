"""Notion rich text -> Mnemo spans."""

from __future__ import annotations

import unittest

from notion2mnemo.colors import ColorMap
from notion2mnemo.mnemo import EquationSpan, TextSpan
from notion2mnemo.richtext import block_color_style, convert_rich_text, rich_text_to_plain

COLORS = ColorMap()


def text(content: str, **annotations):
    base = {
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "code": False,
        "color": "default",
    }
    base.update(annotations)
    return {
        "type": "text",
        "text": {"content": content, "link": None},
        "annotations": base,
        "plain_text": content,
        "href": None,
    }


class Annotations(unittest.TestCase):
    def test_flags_map_one_to_one(self):
        spans = convert_rich_text(
            [text("x", bold=True, italic=True, underline=True, strikethrough=True, code=True)],
            COLORS,
        )
        style = spans[0].style
        self.assertTrue(
            all([style.bold, style.italic, style.underline, style.strikethrough, style.code])
        )

    def test_link_becomes_the_link_url_field(self):
        item = text("Mnemo")
        item["href"] = "https://mnemo.one"
        item["text"]["link"] = {"url": "https://mnemo.one"}
        self.assertEqual(convert_rich_text([item], COLORS)[0].style.link_url, "https://mnemo.one")

    def test_adjacent_identical_runs_are_merged(self):
        spans = convert_rich_text([text("foo"), text("bar")], COLORS)
        self.assertEqual([s.text for s in spans], ["foobar"])


class Colors(unittest.TestCase):
    def test_text_colour_becomes_a_foreground_token(self):
        style = convert_rich_text([text("x", color="red")], COLORS)[0].style
        self.assertEqual(style.foreground_color, "swatch5")
        self.assertIsNone(style.background_color)

    def test_background_colour_becomes_a_background_token(self):
        style = convert_rich_text([text("x", color="yellow_background")], COLORS)[0].style
        self.assertEqual(style.background_color, "swatch7")
        self.assertIsNone(style.foreground_color)

    def test_background_colour_is_not_collapsed_into_the_highlight_flag(self):
        # Notion has nine background colours and Mnemo's `highlight` boolean is
        # one highlighter; mapping to tokens is what keeps the specific hue.
        style = convert_rich_text([text("x", color="blue_background")], COLORS)[0].style
        self.assertFalse(style.highlight)
        self.assertEqual(style.background_color, "swatch9")

    def test_default_colour_sets_neither_field(self):
        style = convert_rich_text([text("x")], COLORS)[0].style
        self.assertIsNone(style.foreground_color)
        self.assertIsNone(style.background_color)

    def test_unknown_colour_degrades_to_none(self):
        style = convert_rich_text([text("x", color="chartreuse")], COLORS)[0].style
        self.assertIsNone(style.foreground_color)

    def test_block_colour_is_pushed_into_runs_that_have_none(self):
        # Notion says "this whole paragraph is red" on the block; Mnemo has no
        # block colour, only per-span style.
        forced = block_color_style("green", COLORS)
        spans = convert_rich_text([text("a"), text("b", color="red")], COLORS, force_style=forced)
        self.assertEqual(spans[0].style.foreground_color, "swatch6")
        self.assertEqual(spans[1].style.foreground_color, "swatch5")

    def test_overrides_replace_the_built_in_mapping(self):
        custom = ColorMap(text={"red": "swatch10"})
        style = convert_rich_text([text("x", color="red")], custom)[0].style
        self.assertEqual(style.foreground_color, "swatch10")

    def test_an_override_to_empty_drops_the_colour(self):
        custom = ColorMap(text={"gray": ""})
        style = convert_rich_text([text("x", color="gray")], custom)[0].style
        self.assertIsNone(style.foreground_color)


class Equations(unittest.TestCase):
    def test_inline_equation_becomes_an_atom(self):
        item = {
            "type": "equation",
            "equation": {"expression": "\\frac{a}{b}"},
            "annotations": {"bold": False, "italic": False, "strikethrough": False,
                            "underline": False, "code": False, "color": "default"},
            "plain_text": "\\frac{a}{b}",
            "href": None,
        }
        spans = convert_rich_text([item], COLORS)
        self.assertIsInstance(spans[0], EquationSpan)
        self.assertEqual(spans[0].latex, "\\frac{a}{b}")

    def test_a_styled_equation_keeps_its_marks(self):
        item = {
            "type": "equation",
            "equation": {"expression": "x^2"},
            "annotations": {"bold": True, "italic": False, "strikethrough": False,
                            "underline": False, "code": False, "color": "blue"},
            "plain_text": "x^2",
            "href": None,
        }
        span = convert_rich_text([item], COLORS)[0]
        self.assertTrue(span.style.bold)
        self.assertEqual(span.style.foreground_color, "swatch3")

    def test_equation_between_words_stays_one_atom_in_a_sentence(self):
        spans = convert_rich_text(
            [
                text("Given "),
                {"type": "equation", "equation": {"expression": "n>0"},
                 "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                 "underline": False, "code": False, "color": "default"},
                 "plain_text": "n>0", "href": None},
                text(", we have"),
            ],
            COLORS,
        )
        self.assertEqual(len(spans), 3)
        self.assertIsInstance(spans[1], EquationSpan)


class Mentions(unittest.TestCase):
    def test_page_mention_keeps_its_label_and_link(self):
        item = {
            "type": "mention",
            "mention": {"type": "page", "page": {"id": "abc"}},
            "annotations": {"bold": False, "italic": False, "strikethrough": False,
                            "underline": False, "code": False, "color": "default"},
            "plain_text": "Design notes",
            "href": "https://www.notion.so/abc",
        }
        span = convert_rich_text([item], COLORS)[0]
        self.assertIsInstance(span, TextSpan)
        self.assertEqual(span.text, "Design notes")
        self.assertEqual(span.style.link_url, "https://www.notion.so/abc")

    def test_date_mention_without_plain_text_still_renders(self):
        item = {
            "type": "mention",
            "mention": {"type": "date", "date": {"start": "2026-01-01", "end": "2026-01-05"}},
            "annotations": {"bold": False, "italic": False, "strikethrough": False,
                            "underline": False, "code": False, "color": "default"},
            "plain_text": "",
            "href": None,
        }
        self.assertEqual(convert_rich_text([item], COLORS)[0].text, "2026-01-01 - 2026-01-05")


class Plain(unittest.TestCase):
    def test_plain_text_joins_every_run(self):
        self.assertEqual(rich_text_to_plain([text("a"), text("b")]), "ab")

    def test_none_is_empty(self):
        self.assertEqual(rich_text_to_plain(None), "")


if __name__ == "__main__":
    unittest.main()
