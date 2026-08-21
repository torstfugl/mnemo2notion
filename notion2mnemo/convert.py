"""
Notion blocks -> Mnemo blocks.

Most of the mapping is one-to-one and unremarkable. The parts that are not, and
the reasoning behind each, are:

* **Colour lives on the block in Notion and on the run in Mnemo.** Notion says
  "this whole paragraph is red" with a block-level ``color`` field; Mnemo has no
  such field, only per-span style. So a block colour is pushed down into every
  span the block produces, which is why ``block_color_style`` is threaded through
  the rich-text conversion rather than applied afterwards.

* **Columns.** Notion allows any number; Mnemo's ``TwoColumn`` holds exactly two,
  enforced by its schema because three separate C# readers index ``Children[0]``
  and ``Children[1]`` positionally. Three columns are therefore nested rather
  than truncated: left, then a nested split holding the rest. Mnemo's column cell
  accepts any block including another ``TwoColumn``, so this round-trips.

* **Toggles.** Mnemo has no collapsible block. A toggle becomes its summary as a
  ``Text`` block with the toggle's contents as children, so the hierarchy and
  every word survive - only the collapse does not.

* **Blocks with no Mnemo equivalent** (bookmarks, embeds, videos, files) become a
  link, never nothing. Silently dropping a block is the one failure mode that
  cannot be noticed after the fact.

Nothing here fetches: children arrive through the injected ``children_of``, which
the CLI wires to the API client and the tests wire to a dict. That is what lets
the whole mapping be tested offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import mnemo
from .assets import AssetStore, file_url
from .colors import ColorMap
from .mnemo import Block, InlineSpan, TextSpan, TextStyle, plain
from .richtext import block_color_style, convert_rich_text, rich_text_to_plain

#: Notion code language -> Mnemo code language token. Mnemo's picker list lives
#: in `mnemo-web/src/notes/editor/code/languages.ts`; a language absent from it
#: is passed through unchanged, because Mnemo shows an unknown token verbatim
#: rather than mislabelling the snippet as plain text.
CODE_LANGUAGES: dict[str, str] = {
    "plain text": "text",
    "c++": "cpp",
    "c#": "csharp",
    "objective-c": "objectivec",
    "shell": "bash",
    "docker": "bash",
    "makefile": "bash",
    "markup": "html",
    "vb.net": "text",
    "visual basic": "text",
    "f#": "text",
    "webassembly": "text",
    "java/c/c++/c#": "java",
    "sass": "css",
    "scss": "css",
    "less": "css",
    "protobuf": "text",
    "mermaid": "text",
}

#: Notion block types that carry no content Mnemo can hold and no content worth
#: a placeholder: a table of contents and a breadcrumb are both generated views
#: of structure that Mnemo renders its own way.
SILENTLY_DROPPED = frozenset({"table_of_contents", "breadcrumb", "template"})

_DEFAULT_CALLOUT_EMOJI = "\N{ELECTRIC LIGHT BULB}"
_WARN_CALLOUT_EMOJI = "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"


@dataclass
class ConversionStats:
    blocks_in: int = 0
    blocks_out: int = 0
    images: int = 0
    equations: int = 0
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, kind: str) -> None:
        self.dropped[kind] = self.dropped.get(kind, 0) + 1


class BlockConverter:
    """
    Converts one Notion page's block tree.

    :param children_of: returns a Notion block's children, given its id.
    :param note_id_for_page: the Mnemo note id a Notion page id was converted to,
        or None when that page is outside the export. Sub-page and page-link
        blocks become real Mnemo ``Page`` blocks when it resolves, and a link
        when it does not.
    """

    def __init__(
        self,
        *,
        colors: ColorMap,
        assets: AssetStore,
        children_of: Callable[[str], list[dict[str, Any]]],
        note_id_for_page: Callable[[str], str | None],
        warnings: list[str] | None = None,
        stats: ConversionStats | None = None,
        max_depth: int = 24,
    ) -> None:
        self.colors = colors
        self.assets = assets
        self.children_of = children_of
        self.note_id_for_page = note_id_for_page
        self.warnings = warnings if warnings is not None else []
        self.stats = stats or ConversionStats()
        self.max_depth = max_depth

    # -- entry point ------------------------------------------------------

    def convert_blocks(self, blocks: Sequence[dict[str, Any]], depth: int = 0) -> list[Block]:
        out: list[Block] = []
        for notion_block in blocks:
            out.extend(self.convert_block(notion_block, depth))
        return out

    def convert_children_of(self, block_id: str, depth: int) -> list[Block]:
        if depth >= self.max_depth:
            # Notion permits deeper nesting than any layout can show. Stopping
            # with a warning beats a recursion error halfway through a workspace.
            self.warnings.append(f"stopped at nesting depth {self.max_depth} in block {block_id}")
            return []
        return self.convert_blocks(self.children_of(block_id), depth + 1)

    # -- one block --------------------------------------------------------

    def convert_block(self, notion: dict[str, Any], depth: int = 0) -> list[Block]:
        kind = notion.get("type") or ""
        self.stats.blocks_in += 1
        handler = getattr(self, f"_{kind}", None)
        if handler is None:
            if kind in SILENTLY_DROPPED:
                self.stats.drop(kind)
                return []
            self.stats.drop(kind)
            self.warnings.append(f"unsupported Notion block type '{kind}' was skipped")
            return []
        blocks = handler(notion, notion.get(kind) or {}, depth)
        self.stats.blocks_out += len(blocks)
        return blocks

    # -- helpers ----------------------------------------------------------

    def _spans(self, data: dict[str, Any], key: str = "rich_text") -> list[InlineSpan]:
        spans = convert_rich_text(
            data.get(key), self.colors, force_style=block_color_style(data.get("color"), self.colors)
        )
        self.stats.equations += sum(1 for span in spans if not isinstance(span, TextSpan))
        return spans

    def _kids(self, notion: dict[str, Any], depth: int) -> list[Block]:
        if not notion.get("has_children"):
            return []
        block_id = notion.get("id") or ""
        return self.convert_children_of(block_id, depth) if block_id else []

    def _simple(
        self, notion: dict[str, Any], data: dict[str, Any], depth: int, block_type: str, payload=None
    ) -> list[Block]:
        return [
            Block(
                type=block_type,
                spans=self._spans(data),
                payload=payload or mnemo.empty_payload(),
                children=self._kids(notion, depth),
                id=mnemo.stable_id("block", notion.get("id") or ""),
            )
        ]

    def _link_block(self, text: str, url: str, notion_id: str, prefix: str = "") -> list[Block]:
        """The fallback for a Notion block Mnemo has no shape for: keep the link."""
        label = f"{prefix}{text or url}"
        spans: list[InlineSpan] = (
            [TextSpan(text=label, style=TextStyle(link_url=url))] if url else [plain(label)]
        )
        return [Block(type=mnemo.TEXT, spans=spans, id=mnemo.stable_id("block", notion_id))]

    # -- handlers (named after the Notion block type) ---------------------

    def _paragraph(self, notion, data, depth):
        return self._simple(notion, data, depth, mnemo.TEXT)

    def _heading_1(self, notion, data, depth):
        return self._simple(notion, data, depth, mnemo.HEADING1)

    def _heading_2(self, notion, data, depth):
        return self._simple(notion, data, depth, mnemo.HEADING2)

    def _heading_3(self, notion, data, depth):
        return self._simple(notion, data, depth, mnemo.HEADING3)

    def _bulleted_list_item(self, notion, data, depth):
        return self._simple(notion, data, depth, mnemo.BULLET_LIST)

    def _numbered_list_item(self, notion, data, depth):
        return self._simple(notion, data, depth, mnemo.NUMBERED_LIST)

    def _to_do(self, notion, data, depth):
        return self._simple(
            notion, data, depth, mnemo.CHECKLIST, mnemo.checklist_payload(bool(data.get("checked")))
        )

    def _quote(self, notion, data, depth):
        return self._simple(notion, data, depth, mnemo.QUOTE)

    def _toggle(self, notion, data, depth):
        # No collapsible block in Mnemo: the summary becomes an ordinary
        # paragraph and the contents stay nested under it, so nothing is lost
        # except the ability to fold it away.
        return self._simple(notion, data, depth, mnemo.TEXT)

    def _divider(self, notion, data, depth):
        return [Block(type=mnemo.DIVIDER, id=mnemo.stable_id("block", notion.get("id") or ""))]

    def _callout(self, notion, data, depth):
        icon = data.get("icon") or {}
        warn = ColorMap.is_warn(data.get("color"))
        emoji = icon.get("emoji") if icon.get("type") == "emoji" else None
        if not emoji:
            # A file or external icon has no emoji form; the tone picks the glyph
            # Mnemo's own insert menu would have used.
            emoji = _WARN_CALLOUT_EMOJI if warn else _DEFAULT_CALLOUT_EMOJI
        return self._simple(
            notion,
            data,
            depth,
            mnemo.CALLOUT,
            mnemo.callout_payload(emoji, "warn" if warn else "note"),
        )

    def _code(self, notion, data, depth):
        source = rich_text_to_plain(data.get("rich_text"))
        language = str(data.get("language") or "").lower()
        caption = rich_text_to_plain(data.get("caption"))
        return [
            Block(
                type=mnemo.CODE,
                # Mnemo keeps the source in the payload and a copy in the spans;
                # its reader backfills one from the other, and writing both keeps
                # the block identical to one typed in the app.
                spans=[plain(source)],
                payload=mnemo.code_payload(
                    CODE_LANGUAGES.get(language, language or "text"), source, caption=caption
                ),
                id=mnemo.stable_id("block", notion.get("id") or ""),
            )
        ]

    def _equation(self, notion, data, depth):
        latex = str(data.get("expression") or "")
        self.stats.equations += 1
        return [
            Block(
                type=mnemo.EQUATION,
                # An equation block renders entirely from its payload; Mnemo's
                # reader forces the spans blank, so carrying text here would only
                # create content nothing reads.
                spans=[plain("")],
                payload=mnemo.equation_payload(latex),
                id=mnemo.stable_id("block", notion.get("id") or ""),
            )
        ]

    def _image(self, notion, data, depth):
        url = file_url(data)
        caption_spans = convert_rich_text(data.get("caption"), self.colors)
        caption = rich_text_to_plain(data.get("caption"))
        asset_id = self.assets.add(url, label=caption or url)
        if asset_id is None:
            return self._link_block(caption or "image", url, notion.get("id") or "", prefix="")
        self.stats.images += 1
        return [
            Block(
                type=mnemo.IMAGE,
                # The caption lives in the block's line and is mirrored into
                # `alt`; Mnemo treats the line as authoritative and rewrites
                # `alt` from it on every save.
                spans=caption_spans or [plain("")],
                payload=mnemo.image_payload(asset_id, alt=caption),
                id=mnemo.stable_id("block", notion.get("id") or ""),
            )
        ]

    def _video(self, notion, data, depth):
        return self._media(notion, data, "video")

    def _audio(self, notion, data, depth):
        return self._media(notion, data, "audio")

    def _pdf(self, notion, data, depth):
        return self._media(notion, data, "PDF")

    def _file(self, notion, data, depth):
        return self._media(notion, data, "file")

    def _media(self, notion: dict[str, Any], data: dict[str, Any], noun: str) -> list[Block]:
        url = file_url(data)
        label = rich_text_to_plain(data.get("caption")) or data.get("name") or f"{noun}"
        if not url:
            self.warnings.append(f"{noun} block had no URL and was skipped")
            self.stats.drop(noun)
            return []
        if url.startswith("https://prod-files-secure") or "amazonaws.com" in url:
            # An uploaded file's URL is signed and expires within the hour, so a
            # link to it is dead on arrival. Say so rather than storing a link
            # that will 403 tomorrow.
            self.warnings.append(
                f"{noun} '{label}' is a Notion-hosted upload; its link expires. "
                "Download it from Notion and attach it manually."
            )
        return self._link_block(label, url, notion.get("id") or "")

    def _bookmark(self, notion, data, depth):
        url = str(data.get("url") or "")
        label = rich_text_to_plain(data.get("caption")) or url
        return self._link_block(label, url, notion.get("id") or "")

    def _embed(self, notion, data, depth):
        url = str(data.get("url") or "")
        label = rich_text_to_plain(data.get("caption")) or url
        return self._link_block(label, url, notion.get("id") or "")

    def _link_preview(self, notion, data, depth):
        url = str(data.get("url") or "")
        return self._link_block(url, url, notion.get("id") or "")

    def _synced_block(self, notion, data, depth):
        # Both the original and its copies return the synced content from the
        # children endpoint, so a synced block is simply inlined - which is also
        # the only thing Mnemo can represent.
        return self._kids(notion, depth)

    def _child_page(self, notion, data, depth):
        page_id = notion.get("id") or ""
        note_id = self.note_id_for_page(page_id)
        if note_id:
            return [
                Block(
                    type=mnemo.PAGE,
                    spans=[plain("")],
                    payload=mnemo.page_payload(note_id),
                    id=mnemo.stable_id("block", page_id),
                )
            ]
        title = str(data.get("title") or "Untitled")
        self.warnings.append(f"sub-page '{title}' is outside the export; kept as a heading")
        return [
            Block(
                type=mnemo.HEADING3,
                spans=[plain(title)],
                id=mnemo.stable_id("block", page_id),
            )
        ]

    def _child_database(self, notion, data, depth):
        # The rows become notes in a folder of their own (see `walker.py`); the
        # page keeps a labelled marker so the reader knows what used to sit here.
        title = str(data.get("title") or "Database")
        return [
            Block(
                type=mnemo.TEXT,
                spans=[TextSpan(text=title, style=TextStyle(bold=True, italic=True))],
                id=mnemo.stable_id("block", notion.get("id") or ""),
            )
        ]

    def _link_to_page(self, notion, data, depth):
        target = data.get("page_id") or data.get("database_id") or ""
        note_id = self.note_id_for_page(str(target)) if target else None
        if note_id:
            return [
                Block(
                    type=mnemo.PAGE,
                    spans=[plain("")],
                    payload=mnemo.page_payload(note_id),
                    id=mnemo.stable_id("block", notion.get("id") or ""),
                )
            ]
        url = f"https://www.notion.so/{str(target).replace('-', '')}" if target else ""
        return self._link_block("Linked page", url, notion.get("id") or "")

    def _table(self, notion, data, depth):
        rows_notion = self.children_of(notion.get("id") or "") if notion.get("has_children") else []
        width = int(data.get("table_width") or 0)
        rows: list[Block] = []
        for row_index, row_notion in enumerate(rows_notion):
            if row_notion.get("type") != "table_row":
                continue
            cells_data = (row_notion.get("table_row") or {}).get("cells") or []
            width = max(width, len(cells_data))
            cells = [
                Block(
                    type=mnemo.TABLE_CELL,
                    spans=convert_rich_text(cell, self.colors),
                    payload=mnemo.table_cell_payload(),
                    id=mnemo.stable_id("cell", row_notion.get("id") or "", str(cell_index)),
                )
                for cell_index, cell in enumerate(cells_data)
            ]
            if not cells:
                cells = [
                    Block(
                        type=mnemo.TABLE_CELL,
                        payload=mnemo.table_cell_payload(),
                        id=mnemo.stable_id("cell", row_notion.get("id") or "", "0"),
                    )
                ]
            rows.append(
                Block(
                    type=mnemo.TABLE_ROW,
                    spans=[plain("")],
                    children=cells,
                    id=mnemo.stable_id("row", row_notion.get("id") or "", str(row_index)),
                )
            )

        if not rows:
            self.stats.drop("table")
            self.warnings.append("a table had no rows and was skipped")
            return []

        # Notion names these from the reader's point of view: `has_column_header`
        # means the first *row* holds the column labels, and `has_row_header`
        # means the first *column* holds the row labels. Mnemo stores a flag per
        # row and per column, so each becomes position 0 of its axis.
        header_rows = [i == 0 and bool(data.get("has_column_header")) for i in range(len(rows))]
        header_columns = [i == 0 and bool(data.get("has_row_header")) for i in range(max(width, 1))]
        return [
            Block(
                type=mnemo.TABLE,
                spans=[plain("")],
                payload=mnemo.table_payload([], header_rows, header_columns),
                children=rows,
                id=mnemo.stable_id("block", notion.get("id") or ""),
            )
        ]

    def _table_row(self, notion, data, depth):
        # Only reachable if a row arrives outside its table, which the API does
        # not do. Handled so it degrades to text rather than a warning storm.
        cells = (data.get("cells") or [])
        text = " | ".join(rich_text_to_plain(cell) for cell in cells)
        return [Block(type=mnemo.TEXT, spans=[plain(text)], id=mnemo.stable_id("block", notion.get("id") or ""))]

    def _column_list(self, notion, data, depth):
        columns_notion = self.children_of(notion.get("id") or "") if notion.get("has_children") else []
        columns_notion = [c for c in columns_notion if c.get("type") == "column"]
        if not columns_notion:
            return []

        contents = [self.convert_children_of(column.get("id") or "", depth) for column in columns_notion]
        ratios = [
            float((column.get("column") or {}).get("width_ratio") or 0) or 1.0 / len(columns_notion)
            for column in columns_notion
        ]

        if len(contents) == 1:
            # One column is not a layout; splicing its contents in beats a
            # `TwoColumn` with an empty lane.
            return contents[0]
        return [self._split(contents, ratios, notion.get("id") or "", 0)]

    def _split(
        self, contents: list[list[Block]], ratios: list[float], notion_id: str, index: int
    ) -> Block:
        """
        Builds a right-nested chain of two-column splits.

        Mnemo's ``TwoColumn`` holds exactly two cells, enforced by its schema, so
        N columns become N-1 nested splits. The ratio at each level is the left
        column's share of everything still to its right, which keeps the visual
        widths proportional to Notion's all the way down.
        """
        left, rest = contents[0], contents[1:]
        remaining = sum(ratios) or 1.0
        ratio = max(0.1, min(0.9, (ratios[0] or 0.0) / remaining))
        right_blocks = (
            rest[0] if len(rest) == 1 else [self._split(rest, ratios[1:], notion_id, index + 1)]
        )
        return Block(
            type=mnemo.TWO_COLUMN,
            spans=[plain("")],
            payload=mnemo.two_column_payload(ratio),
            children=[
                self._column_group(left, notion_id, index, "l"),
                self._column_group(right_blocks, notion_id, index, "r"),
            ],
            id=mnemo.stable_id("block", notion_id, str(index)),
        )

    def _column_group(self, blocks: list[Block], notion_id: str, index: int, side: str) -> Block:
        # Mnemo repairs an empty column cell by seeding a Text block into it on
        # the first transaction; doing it here means the imported note is already
        # in the shape the editor would put it in.
        return Block(
            type=mnemo.COLUMN_GROUP,
            spans=[plain("")],
            children=blocks or [Block(type=mnemo.TEXT, id=mnemo.stable_id("seed", notion_id, str(index), side))],
            id=mnemo.stable_id("col", notion_id, str(index), side),
        )

    def _column(self, notion, data, depth):
        # A column outside a column_list: inline it.
        return self._kids(notion, depth)

    def _unsupported(self, notion, data, depth):
        self.stats.drop("unsupported")
        self.warnings.append(
            "a block Notion itself reports as unsupported by its API was skipped "
            "(usually a database view, button, or AI block)"
        )
        return []
