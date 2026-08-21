"""
Mnemo blocks -> Notion blocks: the reverse mapping.

This is the direction that keeps a user *out* of lock-in, so its bar is the
same as the forward one: nothing silently dropped, and formatting carried
wherever Notion can hold it. The asymmetries all come from Notion's side and
are worth naming, because each one is a deliberate rule here rather than an
accident:

* **Notion allows one colour per text run** - a run has either a text colour or
  a background, never both. A Mnemo span can carry both tokens at once. The
  background wins, because a highlight is the more salient of the two and
  losing the tint of the words under a highlight is the smaller lie.

* **Colour is a nine-name enum, not a token.** The swatch tokens map back onto
  Notion's palette by hue, the inverse of ``colors.py``. Two Mnemo tokens that
  the forward direction collapsed (pink and red both land on ``swatch5``) come
  back as the single Notion colour they are closest to - a round trip through
  both tools converges rather than drifting.

* **Rich text runs cap at 2000 characters** and an equation expression at 1000.
  Long spans are split mid-run with identical annotations, which Notion renders
  identically; an over-long equation degrades to a code run with a warning
  rather than a 400 for the whole page.

* **Notion nests at most two levels per write request.** This module therefore
  does not emit trees: it emits one node per Mnemo block with its children kept
  separate, and marks the node kinds whose children *must* ride along inline
  (table rows in a table, columns in a column list). The writer in ``push.py``
  owns the batching.

The functions here are pure - dicts in, dicts out, warnings appended to the
context - so the whole mapping is testable without a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

#: Notion's per-rich-text content cap.
MAX_TEXT_LENGTH = 2000
#: Notion's cap on an equation expression.
MAX_EQUATION_LENGTH = 1000

#: Mnemo foreground swatch token -> Notion text colour. Inverse of
#: `colors.TEXT_COLORS`, extended to the tokens the forward direction never
#: produces so a hand-coloured note still comes through.
FOREGROUND_TO_NOTION: dict[str, str] = {
    "swatch1": "gray",    # stone
    "swatch2": "purple",  # violet
    "swatch3": "blue",
    "swatch4": "purple",
    "swatch5": "red",
    "swatch6": "green",
    "swatch7": "yellow",  # goldenrod
    "swatch8": "orange",
    "swatch9": "blue",
    "swatch10": "green",  # teal
}

#: Mnemo background swatch token -> Notion background colour.
BACKGROUND_TO_NOTION: dict[str, str] = {
    "swatch1": "gray_background",
    "swatch2": "purple_background",
    "swatch3": "blue_background",
    "swatch4": "purple_background",
    "swatch5": "red_background",    # blush sits nearer Notion's red tint than its pink
    "swatch6": "green_background",
    "swatch7": "yellow_background",
    "swatch8": "orange_background",
    "swatch9": "blue_background",
    "swatch10": "green_background",
}

#: Mnemo code language token -> Notion code block language. Only the spellings
#: that differ; everything else passes through and Notion falls back to plain
#: text for a name it does not know.
CODE_LANGUAGES_TO_NOTION: dict[str, str] = {
    "text": "plain text",
    "cpp": "c++",
    "csharp": "c#",
    "objectivec": "objective-c",
    "bash": "shell",
    "html": "html",
    "matlab": "matlab",
    "powershell": "powershell",
    "verilog": "verilog",
    "vhdl": "vhdl",
    "toml": "toml",  # not in Notion's list; passes through as-is and degrades gracefully
}

#: Callout tone -> Notion callout colour. Mnemo has exactly two tones.
CALLOUT_TONE_COLORS = {"note": "gray_background", "warn": "red_background"}


@dataclass
class ReverseContext:
    """
    Carried through one note's conversion.

    ``resolve_image`` turns a Mnemo image reference (a bare asset id inside the
    package, or a remote URL) into the Notion file object to embed, or None when
    it cannot - the caller owns uploads because they need a network. ``warnings``
    collects everything that degraded.
    """

    resolve_image: Callable[[str], dict[str, Any] | None]
    warnings: list[str] = field(default_factory=list)


@dataclass
class NotionNode:
    """
    One Notion block plus its children, kept apart for the writer.

    ``inline_children`` marks the kinds whose children must be written in the
    same request (table rows, columns): Notion rejects an empty table and an
    empty column list outright, so these cannot be appended in a second pass.
    ``note_ref`` marks a Mnemo ``Page`` block: it produces no Notion block at
    all - the writer creates a real child page at this position instead.
    """

    block: dict[str, Any] | None
    children: list["NotionNode"] = field(default_factory=list)
    inline_children: bool = False
    note_ref: str | None = None


# ---------------------------------------------------------------------------
# Rich text
# ---------------------------------------------------------------------------


def _annotations(style: dict[str, Any]) -> dict[str, Any]:
    """
    A Mnemo ``TextStyle`` as Notion annotations.

    The one lossy rule lives here: Notion's ``color`` field holds either a text
    colour or a background, and a span carrying both keeps the background. A
    span with the plain ``highlight`` flag (no token) becomes Notion's yellow
    background, which is what a highlighter is there.
    """
    color = "default"
    fg = style.get("foregroundColor")
    bg = style.get("backgroundColor")
    if fg and fg in FOREGROUND_TO_NOTION:
        color = FOREGROUND_TO_NOTION[fg]
    if bg and bg in BACKGROUND_TO_NOTION:
        color = BACKGROUND_TO_NOTION[bg]
    elif style.get("highlight"):
        color = "yellow_background"

    return {
        "bold": bool(style.get("bold")),
        "italic": bool(style.get("italic")),
        "strikethrough": bool(style.get("strikethrough")),
        "underline": bool(style.get("underline")),
        "code": bool(style.get("code")),
        "color": color,
    }


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def span_to_rich_text(span: dict[str, Any], ctx: ReverseContext) -> list[dict[str, Any]]:
    """One Mnemo span as one or more Notion rich-text objects."""
    style = span.get("style") or {}
    annotations = _annotations(style)
    kind = (span.get("kind") or "text").lower()

    if kind == "equation":
        latex = span.get("latex") or ""
        if not latex:
            return []
        if len(latex) > MAX_EQUATION_LENGTH:
            # Losing the whole page over one oversized expression is the wrong
            # trade; a code run keeps the source readable and editable.
            ctx.warnings.append(
                f"an inline equation of {len(latex)} characters exceeds Notion's "
                f"{MAX_EQUATION_LENGTH}-character limit; kept as code text"
            )
            code_annotations = dict(annotations, code=True)
            return [
                {"type": "text", "text": {"content": chunk}, "annotations": code_annotations}
                for chunk in _chunks(latex, MAX_TEXT_LENGTH)
            ]
        return [{"type": "equation", "equation": {"expression": latex}, "annotations": annotations}]

    if kind == "fraction":
        numerator = span.get("numerator", 0)
        denominator = span.get("denominator", 1)
        return [
            {
                "type": "equation",
                "equation": {"expression": f"\\frac{{{numerator}}}{{{denominator}}}"},
                "annotations": annotations,
            }
        ]

    text = span.get("text") or ""
    if not text:
        return []
    link = style.get("linkUrl")
    out = []
    for chunk in _chunks(text, MAX_TEXT_LENGTH):
        item: dict[str, Any] = {"type": "text", "text": {"content": chunk}, "annotations": annotations}
        if link:
            item["text"]["link"] = {"url": link}
        out.append(item)
    return out


def spans_to_rich_text(spans: list[dict[str, Any]] | None, ctx: ReverseContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for span in spans or []:
        out.extend(span_to_rich_text(span, ctx))
    return out


def spans_plain_text(spans: list[dict[str, Any]] | None) -> str:
    parts = []
    for span in spans or []:
        if span.get("kind") == "equation":
            parts.append(span.get("latex") or "")
        else:
            parts.append(span.get("text") or "")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def block_to_notion(block: dict[str, Any], ctx: ReverseContext) -> NotionNode | None:
    """
    One Mnemo block as a NotionNode, or None for a block with nothing to say.

    Children are converted recursively but kept on the node, never embedded in
    the block dict - the writer decides what rides inline and what is appended
    in a follow-up request.
    """
    block_type = block.get("type") or "Text"
    payload = block.get("payload") or {}
    handler = _HANDLERS.get(block_type, _paragraph)
    node = handler(block, payload, ctx)
    if node is None or node.inline_children:
        # Inline-children kinds (table, twoColumn) consumed their children in
        # the handler; anything else would double-write them.
        return node
    for child in block.get("children") or []:
        child_node = block_to_notion(child, ctx)
        if child_node is not None:
            node.children.append(child_node)
    return node


def _rich_body(kind: str, block: dict[str, Any], ctx: ReverseContext, **extra: Any) -> NotionNode:
    body = {"rich_text": spans_to_rich_text(block.get("spans"), ctx), **extra}
    return NotionNode(block={"object": "block", "type": kind, kind: body})


def _paragraph(block, payload, ctx):
    return _rich_body("paragraph", block, ctx)


def _heading(level: int):
    def handler(block, payload, ctx):
        # Notion has three heading levels to Mnemo's four; the fourth keeps its
        # text and demotes rather than disappearing.
        kind = f"heading_{min(level, 3)}"
        return _rich_body(kind, block, ctx)

    return handler


def _bullet(block, payload, ctx):
    return _rich_body("bulleted_list_item", block, ctx)


def _numbered(block, payload, ctx):
    return _rich_body("numbered_list_item", block, ctx)


def _checklist(block, payload, ctx):
    return _rich_body("to_do", block, ctx, checked=bool(payload.get("checked")))


def _quote(block, payload, ctx):
    return _rich_body("quote", block, ctx)


def _divider(block, payload, ctx):
    return NotionNode(block={"object": "block", "type": "divider", "divider": {}})


def _callout(block, payload, ctx):
    tone = payload.get("tone") or "note"
    node = _rich_body(
        "callout",
        block,
        ctx,
        color=CALLOUT_TONE_COLORS.get(tone, "gray_background"),
    )
    emoji = payload.get("emoji") or ""
    if emoji:
        node.block["callout"]["icon"] = {"type": "emoji", "emoji": emoji}
    return node


def _code(block, payload, ctx):
    source = payload.get("source") or spans_plain_text(block.get("spans"))
    language = CODE_LANGUAGES_TO_NOTION.get(payload.get("language") or "", payload.get("language") or "plain text")
    body: dict[str, Any] = {
        "rich_text": [
            {"type": "text", "text": {"content": chunk}} for chunk in _chunks(source, MAX_TEXT_LENGTH)
        ],
        "language": language,
    }
    caption = payload.get("caption") or ""
    if caption:
        body["caption"] = [{"type": "text", "text": {"content": caption[:MAX_TEXT_LENGTH]}}]
    return NotionNode(block={"object": "block", "type": "code", "code": body})


def _equation(block, payload, ctx):
    latex = payload.get("latex") or ""
    if len(latex) > MAX_EQUATION_LENGTH:
        ctx.warnings.append(
            f"a block equation of {len(latex)} characters exceeds Notion's limit; kept as a code block"
        )
        return NotionNode(
            block={
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": [
                        {"type": "text", "text": {"content": chunk}}
                        for chunk in _chunks(latex, MAX_TEXT_LENGTH)
                    ],
                    "language": "latex",
                },
            }
        )
    return NotionNode(block={"object": "block", "type": "equation", "equation": {"expression": latex}})


def _image(block, payload, ctx):
    path = payload.get("path") or ""
    file_object = ctx.resolve_image(path) if path else None
    if file_object is None:
        # The reference could not become a Notion file. Keep the caption (or the
        # reference) as text so the reader can see something stood here.
        caption = spans_plain_text(block.get("spans")) or path or "image"
        ctx.warnings.append(f"image '{path}' could not be carried to Notion; kept as text")
        return NotionNode(
            block={
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": f"[image: {caption[:200]}]"}}
                    ]
                },
            }
        )
    body = dict(file_object)
    caption_rich = spans_to_rich_text(block.get("spans"), ctx)
    if caption_rich and spans_plain_text(block.get("spans")).strip():
        body["caption"] = caption_rich
    return NotionNode(block={"object": "block", "type": "image", "image": body})


def _table(block, payload, ctx):
    rows = []
    width = 0
    for row in block.get("children") or []:
        if row.get("type") != "TableRow":
            continue
        cells = [
            spans_to_rich_text(cell.get("spans"), ctx)
            for cell in row.get("children") or []
            if cell.get("type") == "TableCell"
        ]
        width = max(width, len(cells))
        rows.append(cells)
    if not rows or width == 0:
        return None

    # Notion demands a rectangle.
    for cells in rows:
        while len(cells) < width:
            cells.append([])

    header_rows = payload.get("headerRows") or []
    header_columns = payload.get("headerColumns") or []
    if any(header_rows[1:]) or any(header_columns[1:]):
        # Notion can only mark the first row and first column.
        ctx.warnings.append(
            "a table marks a non-first row or column as a header, which Notion cannot represent"
        )

    row_nodes = [
        NotionNode(
            block={
                "object": "block",
                "type": "table_row",
                "table_row": {"cells": cells},
            }
        )
        for cells in rows
    ]
    table_block = {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": bool(header_rows[0] if header_rows else False),
            "has_row_header": bool(header_columns[0] if header_columns else False),
        },
    }
    # Rows must ride in the same request: Notion rejects an empty table.
    return NotionNode(block=table_block, children=row_nodes, inline_children=True)


def _two_column(block, payload, ctx):
    groups = [child for child in block.get("children") or [] if child.get("type") == "ColumnGroup"]
    if len(groups) < 2:
        # A malformed split degrades to its contents in order. Marked
        # inline_children because the children were consumed right here - the
        # generic wrapper must not convert the column groups a second time.
        node = NotionNode(block=None, inline_children=True)
        for group in groups:
            for child in group.get("children") or []:
                child_node = block_to_notion(child, ctx)
                if child_node is not None:
                    node.children.append(child_node)
        return node

    ratio = payload.get("splitRatio")
    ratios = [ratio, 1 - ratio] if isinstance(ratio, (int, float)) and 0 < ratio < 1 else [None, None]

    column_nodes = []
    for group, width_ratio in zip(groups[:2], ratios):
        children = []
        for child in group.get("children") or []:
            child_node = block_to_notion(child, ctx)
            if child_node is not None:
                children.append(child_node)
        if not children:
            # Notion rejects an empty column outright.
            children = [
                NotionNode(block={"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}})
            ]
        column: dict[str, Any] = {"object": "block", "type": "column", "column": {}}
        if width_ratio is not None:
            column["column"]["width_ratio"] = round(float(width_ratio), 4)
        column_nodes.append(NotionNode(block=column, children=children, inline_children=True))

    return NotionNode(
        block={"object": "block", "type": "column_list", "column_list": {}},
        children=column_nodes,
        inline_children=True,
    )


def _page(block, payload, ctx):
    reference = payload.get("referenceNoteId") or ""
    if not reference:
        return None
    # No block at all: the writer creates a real child page at this position.
    return NotionNode(block=None, note_ref=reference)


def _sketch(block, payload, ctx):
    # Notion has no drawing surface; the sketch's DSL source is at least the
    # editable form of it.
    text = spans_plain_text(block.get("spans"))
    ctx.warnings.append("a sketch block has no Notion equivalent; its source was kept as a code block")
    return NotionNode(
        block={
            "object": "block",
            "type": "code",
            "code": {
                "rich_text": [
                    {"type": "text", "text": {"content": chunk}}
                    for chunk in _chunks(text or "(empty sketch)", MAX_TEXT_LENGTH)
                ],
                "language": "plain text",
            },
        }
    )


_HANDLERS: dict[str, Callable[[dict, dict, ReverseContext], NotionNode | None]] = {
    "Text": _paragraph,
    "Heading1": _heading(1),
    "Heading2": _heading(2),
    "Heading3": _heading(3),
    "Heading4": _heading(4),
    "BulletList": _bullet,
    "NumberedList": _numbered,
    "Checklist": _checklist,
    "Quote": _quote,
    "Code": _code,
    "Divider": _divider,
    "Image": _image,
    "Equation": _equation,
    "Callout": _callout,
    "Table": _table,
    "TwoColumn": _two_column,
    "Page": _page,
    "Sketch": _sketch,
    # A stray cell or row outside a table, or a column group outside a split,
    # degrades to a paragraph carrying its text.
    "TableRow": _paragraph,
    "TableCell": _paragraph,
    "ColumnGroup": _paragraph,
}


def note_to_nodes(note: dict[str, Any], ctx: ReverseContext) -> list[NotionNode]:
    """A whole note's blocks as an ordered list of NotionNodes."""
    nodes = []
    for block in note.get("blocks") or []:
        node = block_to_notion(block, ctx)
        if node is not None:
            nodes.append(node)
    return nodes
