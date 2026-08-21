"""
Pins the emitted JSON against Mnemo's own reader and writer.

These are the tests that would catch a format drift: every assertion here
restates something read out of `BlockJsonConverter.cs` or `wire.ts`, so if Mnemo
changes its format, this file is where the mismatch shows up rather than in a
silent import that loses a note's colours.
"""

from __future__ import annotations

import unittest

from notion2mnemo import mnemo
from notion2mnemo.mnemo import (
    Block,
    EquationSpan,
    Note,
    TextSpan,
    TextStyle,
    normalize_spans,
    plain,
)


class TextStyleJson(unittest.TestCase):
    def test_default_style_omits_nullable_fields(self):
        # WriteTextStyle writes the six booleans and suppressAutoLink always, and
        # the three nullable fields only when set.
        self.assertEqual(
            TextStyle().to_json(),
            {
                "bold": False,
                "italic": False,
                "underline": False,
                "strikethrough": False,
                "code": False,
                "highlight": False,
                "suppressAutoLink": False,
            },
        )

    def test_subscript_and_superscript_are_written_only_when_true(self):
        self.assertNotIn("subscript", TextStyle().to_json())
        self.assertTrue(TextStyle(subscript=True).to_json()["subscript"])

    def test_colours_are_tokens_not_hex(self):
        style = TextStyle(foreground_color="swatch3", background_color="swatch7")
        payload = style.to_json()
        self.assertEqual(payload["foregroundColor"], "swatch3")
        self.assertEqual(payload["backgroundColor"], "swatch7")


class SpanJson(unittest.TestCase):
    def test_text_span_shape(self):
        self.assertEqual(
            TextSpan("hi", TextStyle(bold=True)).to_json()["kind"],
            "text",
        )

    def test_equation_span_carries_latex_not_text(self):
        payload = EquationSpan("e^{i\\pi}").to_json()
        self.assertEqual(payload["kind"], "equation")
        self.assertEqual(payload["latex"], "e^{i\\pi}")
        self.assertNotIn("text", payload)


class Normalization(unittest.TestCase):
    def test_adjacent_same_style_spans_merge(self):
        spans = normalize_spans([plain("foo"), plain("bar")])
        self.assertEqual([s.text for s in spans], ["foobar"])

    def test_differing_styles_stay_separate(self):
        spans = normalize_spans([plain("a"), TextSpan("b", TextStyle(bold=True))])
        self.assertEqual(len(spans), 2)

    def test_atoms_never_merge_across(self):
        spans = normalize_spans([plain("a"), EquationSpan("x"), plain("b")])
        self.assertEqual(len(spans), 3)

    def test_empty_run_still_yields_one_blank_span(self):
        # Mnemo's reader substitutes a blank span for an empty array, so emitting
        # one keeps what we write identical to what round-trips.
        self.assertEqual([s.text for s in normalize_spans([])], [""])

    def test_atom_placeholder_characters_are_stripped(self):
        # U+FFFC and friends are Avalonia caret placeholders; Mnemo logs an error
        # when asked to persist one.
        spans = normalize_spans([plain("a￼b")])
        self.assertEqual(spans[0].text, "ab")


class BlockJson(unittest.TestCase):
    def test_children_omitted_when_empty(self):
        payload = Block(type=mnemo.TEXT).to_json()
        self.assertNotIn("children", payload)

    def test_children_are_ordered_by_position(self):
        block = Block(
            type=mnemo.BULLET_LIST,
            children=[Block(type=mnemo.TEXT), Block(type=mnemo.TEXT)],
        )
        self.assertEqual([c["order"] for c in block.to_json()["children"]], [0, 1])

    def test_sid_is_never_written(self):
        # A sid must be unique within a note and only Mnemo can mint one; an
        # invented value would collide with an id the user has already seen.
        self.assertNotIn("sid", Block(type=mnemo.TEXT).to_json())

    def test_every_block_carries_at_least_one_span(self):
        payload = Block(type=mnemo.DIVIDER, spans=[]).to_json()
        self.assertEqual(len(payload["spans"]), 1)

    def test_payload_kinds_are_all_known_to_the_reader(self):
        # An unrecognised payload kind throws in C#, unlike an unrecognised block
        # type. This is the list ReadPayload switches on.
        known = {
            "empty", "equation", "image", "code", "checklist",
            "twocolumn", "page", "sketch", "table", "tablecell", "callout",
        }
        payloads = [
            mnemo.empty_payload(),
            mnemo.equation_payload("x"),
            mnemo.image_payload("a.png"),
            mnemo.code_payload("python", "x = 1"),
            mnemo.checklist_payload(True),
            mnemo.two_column_payload(0.5),
            mnemo.page_payload("id"),
            mnemo.callout_payload("!", "warn"),
            mnemo.table_payload([], [True], [False]),
            mnemo.table_cell_payload(),
        ]
        for payload in payloads:
            self.assertIn(payload["kind"].lower(), known, payload)


class NoteJson(unittest.TestCase):
    def test_note_field_names_are_camel_case(self):
        note = Note(note_id="n1", title="T")
        payload = note.to_json()
        for key in ("noteId", "folderId", "parentNoteId", "folderPath", "isFavorite"):
            self.assertIn(key, payload)

    def test_timestamps_are_utc_with_a_z_suffix(self):
        payload = Note(note_id="n1", title="T").to_json()
        self.assertTrue(payload["createdAt"].endswith("Z"), payload["createdAt"])

    def test_content_is_flattened_from_blocks(self):
        note = Note(
            note_id="n1",
            title="T",
            blocks=[
                Block(type=mnemo.HEADING1, spans=[plain("Title")]),
                Block(type=mnemo.EQUATION, payload=mnemo.equation_payload("a^2")),
            ],
        )
        self.assertEqual(note.plain_text(), "Title\na^2")

    def test_ids_are_stable_across_runs(self):
        self.assertEqual(mnemo.stable_id("note", "abc"), mnemo.stable_id("note", "abc"))
        self.assertNotEqual(mnemo.stable_id("note", "abc"), mnemo.stable_id("note", "abd"))


if __name__ == "__main__":
    unittest.main()
