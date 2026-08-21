"""
Walks a Notion workspace and produces Mnemo notes and folders.

The interesting decision here is how Notion's shape maps onto Mnemo's, because
the two organise content differently:

* **A Notion sub-page becomes a Mnemo sub-note, not a folder.** Mnemo already has
  this exact relationship - a note with a ``ParentNoteId``, embedded in its
  parent by a ``Page`` block - so sub-pages keep both their nesting and their
  in-page position. Turning them into folders would move the content out of the
  page it was written inside.

* **A Notion database becomes a folder of notes.** Mnemo has no database, and the
  honest reduction of a table of pages is a folder of pages. Row properties are
  not thrown away: the ones that read as labels become Mnemo tags, and the rest
  are rendered as a small property table at the top of the note, so the data is
  still there to read even though it is no longer queryable.

* **Discovery goes through ``/search`` when no explicit ids are given**, which
  returns every page the integration can see - sub-pages and database rows
  included - each carrying its parent. That means the whole hierarchy is known
  before any conversion starts, which is what lets a ``child_page`` block become
  a real ``Page`` block pointing at an id that will exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from . import mnemo
from .assets import AssetStore
from .colors import ColorMap
from .convert import BlockConverter, ConversionStats
from .mnemo import Block, Folder, Note, TextSpan, TextStyle, plain
from .notion import NotionClient
from .richtext import convert_rich_text, rich_text_to_plain

UNTITLED = "Untitled"


@dataclass
class WalkOptions:
    #: Name of the folder every imported note lands under. Empty places them at
    #: the root of the notes tree, mixed in with what is already there.
    root_folder: str = "Notion"
    #: "table" renders a database row's properties as a table at the top of the
    #: note; "none" drops them (tags are still extracted either way).
    database_properties: str = "table"
    include_databases: bool = True
    #: Download page covers. Off by default for large workspaces - covers are
    #: decorative and are the bulkiest thing in a typical export.
    covers: bool = False
    limit: int | None = None


@dataclass
class WalkResult:
    notes: list[Note] = field(default_factory=list)
    folders: list[Folder] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: ConversionStats = field(default_factory=ConversionStats)
    skipped: int = 0


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def page_title(page: dict[str, Any]) -> str:
    """
    A page's title, wherever this kind of page keeps it.

    A database row keeps it in whichever property has type ``title`` (the name of
    that property is the user's choice, so it must be found by type); a plain
    page uses the property literally called ``title``. Both shapes appear in one
    workspace, so both are checked.
    """
    properties = page.get("properties") or {}
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            text = rich_text_to_plain(prop.get("title"))
            if text.strip():
                return text.strip()
    title = properties.get("title")
    if isinstance(title, dict):
        text = rich_text_to_plain(title.get("title"))
        if text.strip():
            return text.strip()
    return UNTITLED


def database_title(database: dict[str, Any]) -> str:
    text = rich_text_to_plain(database.get("title")).strip()
    return text or UNTITLED


def _icon_emoji(node: dict[str, Any]) -> str | None:
    icon = node.get("icon") or {}
    return icon.get("emoji") if icon.get("type") == "emoji" else None


class Walker:
    def __init__(
        self,
        client: NotionClient,
        colors: ColorMap,
        assets: AssetStore,
        options: WalkOptions,
        *,
        progress: Callable[[str], None] = lambda _message: None,
    ) -> None:
        self.client = client
        self.colors = colors
        self.assets = assets
        self.options = options
        self.progress = progress
        self.result = WalkResult()

        self._pages: dict[str, dict[str, Any]] = {}
        self._databases: dict[str, dict[str, Any]] = {}
        #: The pages this run will actually write. Narrower than `_pages` when
        #: --limit is in play, and it is this set that a Page block may point at.
        self._included: set[str] = set()
        #: Notion id -> Mnemo folder id, for databases only.
        self._folder_ids: dict[str, str] = {}
        self._folder_paths: dict[str, str] = {}
        self._root_folder_id: str | None = None

    # -- discovery --------------------------------------------------------

    def discover(self, page_ids: Iterable[str] = (), database_ids: Iterable[str] = ()) -> None:
        page_ids = [normalize_id(i) for i in page_ids]
        database_ids = [normalize_id(i) for i in database_ids]

        if not page_ids and not database_ids:
            self.progress("Searching the workspace...")
            for page in self.client.search_pages():
                self._pages[page["id"]] = page
            if self.options.include_databases:
                for database in self.client.search_databases():
                    self._databases[database["id"]] = database
        else:
            for page_id in page_ids:
                self._collect_page_tree(page_id)
            for database_id in database_ids:
                self._collect_database(database_id)

        self._pages = {k: v for k, v in self._pages.items() if not _is_trashed(v)}
        self._databases = {k: v for k, v in self._databases.items() if not _is_trashed(v)}
        self.progress(
            f"Found {len(self._pages)} page(s) and {len(self._databases)} database(s)."
        )

    def _collect_page_tree(self, page_id: str, depth: int = 0) -> None:
        """
        Explicit-id mode: a page names only itself, so its descendants are found
        by walking its blocks. The client caches, so the conversion pass that
        follows re-reads these children for free.
        """
        if page_id in self._pages or depth > 32:
            return
        try:
            page = self.client.get_page(page_id)
        except Exception as exc:
            self.result.warnings.append(f"could not read page {page_id}: {exc}")
            return
        self._pages[page_id] = page
        for block in self._iter_blocks(page_id):
            kind = block.get("type")
            if kind == "child_page":
                self._collect_page_tree(block["id"], depth + 1)
            elif kind == "child_database" and self.options.include_databases:
                self._collect_database(block["id"], depth + 1)

    def _iter_blocks(self, block_id: str) -> Iterable[dict[str, Any]]:
        """Every descendant block, flattened - discovery does not care about shape."""
        try:
            children = self.client.block_children(block_id)
        except Exception as exc:
            self.result.warnings.append(f"could not read children of {block_id}: {exc}")
            return
        for child in children:
            yield child
            # A child page owns its own subtree; recursing into it here would
            # attribute its descendants to this page.
            if child.get("has_children") and child.get("type") != "child_page":
                yield from self._iter_blocks(child["id"])

    def _collect_database(self, database_id: str, depth: int = 0) -> None:
        if database_id in self._databases:
            return
        try:
            database = self.client.get_database(database_id)
        except Exception as exc:
            self.result.warnings.append(f"could not read database {database_id}: {exc}")
            return
        self._databases[database_id] = database
        try:
            rows = self.client.query_database(database_id)
        except Exception as exc:
            self.result.warnings.append(f"could not query database {database_id}: {exc}")
            return
        for row in rows:
            if row.get("id") and row["id"] not in self._pages:
                self._collect_page_tree(row["id"], depth + 1)

    # -- conversion -------------------------------------------------------

    def convert(self) -> WalkResult:
        self._build_folders()

        pages = list(self._pages.values())
        if self.options.limit is not None:
            pages = pages[: self.options.limit]

        # A `Page` block reads its title from the note it references, so a
        # reference to a page that never gets written renders as nothing. With
        # --limit truncating the list, "discovered" and "converted" are not the
        # same set, and only the second one may be linked to.
        self._included = {page["id"] for page in pages}

        for index, page in enumerate(pages, start=1):
            title = page_title(page)
            self.progress(f"[{index}/{len(pages)}] {title}")
            try:
                self.result.notes.append(self._convert_page(page, index - 1))
            except Exception as exc:
                self.result.skipped += 1
                self.result.warnings.append(f"failed to convert page '{title}': {exc}")

        self.result.warnings.extend(self.assets.warnings)
        return self.result

    def _build_folders(self) -> None:
        if self.options.root_folder:
            self._root_folder_id = mnemo.stable_id("folder", "root", self.options.root_folder)
            self.result.folders.append(
                Folder(folder_id=self._root_folder_id, name=self.options.root_folder, order=0)
            )
            self._folder_paths[self._root_folder_id] = self.options.root_folder

        # Databases become folders. A database nested inside a page is placed
        # beside that page rather than under it, because Mnemo folders hold notes,
        # not other notes' content, and a page is a note.
        for order, (database_id, database) in enumerate(sorted(self._databases.items())):
            folder_id = mnemo.stable_id("folder", database_id)
            parent_id = self._root_folder_id
            name = database_title(database)
            self._folder_ids[database_id] = folder_id
            self.result.folders.append(
                Folder(folder_id=folder_id, name=name, parent_id=parent_id, order=order + 1)
            )
            parent_path = self._folder_paths.get(parent_id or "", "")
            self._folder_paths[folder_id] = f"{parent_path} / {name}" if parent_path else name

    def _note_id_for_page(self, notion_page_id: str) -> str | None:
        normalized = normalize_id(notion_page_id)
        for candidate in (notion_page_id, normalized, _dashed(normalized)):
            if candidate in self._included:
                return mnemo.stable_id("note", candidate)
        return None

    def _convert_page(self, page: dict[str, Any], order: int) -> Note:
        page_id = page["id"]
        parent = page.get("parent") or {}
        parent_kind = parent.get("type")

        folder_id = self._root_folder_id
        parent_note_id: str | None = None

        if parent_kind in {"database_id", "data_source_id"}:
            database_id = parent.get("database_id") or parent.get("data_source_id") or ""
            folder_id = self._folder_ids.get(database_id, self._root_folder_id)
        elif parent_kind == "page_id":
            parent_page_id = parent.get("page_id") or ""
            parent_note_id = self._note_id_for_page(parent_page_id)
            # A sub-note lives beside its parent in the tree; Mnemo nests it by
            # ParentNoteId, not by folder.
            parent_page = self._pages.get(parent_page_id)
            if parent_page is not None:
                folder_id = self._folder_for_page(parent_page)
        elif parent_kind == "block_id":
            # A page created inside a column or toggle. Its owning page is not in
            # the parent chain, so it lands at the root of the import.
            folder_id = self._root_folder_id

        converter = BlockConverter(
            colors=self.colors,
            assets=self.assets,
            children_of=self.client.block_children,
            note_id_for_page=self._note_id_for_page,
            warnings=self.result.warnings,
            stats=self.result.stats,
        )

        blocks: list[Block] = []
        if parent_kind in {"database_id", "data_source_id"} and self.options.database_properties == "table":
            property_table = self._property_table(page)
            if property_table is not None:
                blocks.append(property_table)
        blocks.extend(converter.convert_blocks(self.client.block_children(page_id)))
        if not blocks:
            # An empty Notion page still needs one editable block, and it needs a
            # stable id like every other: a random one would make each export
            # differ from the last for a page nobody touched.
            blocks = [Block(type=mnemo.TEXT, id=mnemo.stable_id("empty", page_id))]

        cover = None
        if self.options.covers:
            cover_url = _cover_url(page)
            if cover_url:
                asset_id = self.assets.add(cover_url, label=f"cover of {page_title(page)}")
                if asset_id:
                    cover = f"asset:{asset_id}"

        return Note(
            note_id=mnemo.stable_id("note", page_id),
            title=page_title(page),
            blocks=blocks,
            folder_id=folder_id,
            folder_path=self._folder_paths.get(folder_id or "", ""),
            parent_note_id=parent_note_id,
            order=order,
            emoji=_icon_emoji(page),
            cover=cover,
            tags=_tags(page),
            created_at=_parse_time(page.get("created_time")),
            modified_at=_parse_time(page.get("last_edited_time")),
        )

    def _folder_for_page(self, page: dict[str, Any]) -> str | None:
        parent = page.get("parent") or {}
        if parent.get("type") in {"database_id", "data_source_id"}:
            database_id = parent.get("database_id") or parent.get("data_source_id") or ""
            return self._folder_ids.get(database_id, self._root_folder_id)
        if parent.get("type") == "page_id":
            grandparent = self._pages.get(parent.get("page_id") or "")
            if grandparent is not None and grandparent is not page:
                return self._folder_for_page(grandparent)
        return self._root_folder_id

    # -- database properties ---------------------------------------------

    def _property_table(self, page: dict[str, Any]) -> Block | None:
        """
        A database row's properties as a two-column table at the top of the note.

        Mnemo has no database, so a row's fields would otherwise vanish entirely -
        which for many workspaces is where the actual information lives (status,
        due date, source, author). A table keeps every field readable and keeps it
        out of the way of the page body.
        """
        page_id = page.get("id") or ""
        rows: list[Block] = []
        for name, prop in sorted((page.get("properties") or {}).items()):
            if not isinstance(prop, dict) or prop.get("type") == "title":
                continue
            value_spans = self._property_spans(prop)
            if not value_spans:
                continue
            # Keyed by property name rather than row index, so adding a property
            # in Notion does not renumber the ids of the ones after it.
            rows.append(
                Block(
                    type=mnemo.TABLE_ROW,
                    id=mnemo.stable_id("prop-row", page_id, name),
                    children=[
                        Block(
                            type=mnemo.TABLE_CELL,
                            spans=[TextSpan(text=name, style=TextStyle(bold=True))],
                            payload=mnemo.table_cell_payload(),
                            id=mnemo.stable_id("prop-key", page_id, name),
                        ),
                        Block(
                            type=mnemo.TABLE_CELL,
                            spans=value_spans,
                            payload=mnemo.table_cell_payload(),
                            id=mnemo.stable_id("prop-value", page_id, name),
                        ),
                    ],
                )
            )
        if not rows:
            return None
        return Block(
            type=mnemo.TABLE,
            payload=mnemo.table_payload([], [False] * len(rows), [True, False]),
            children=rows,
            id=mnemo.stable_id("prop-table", page_id),
        )

    def _property_spans(self, prop: dict[str, Any]) -> list:
        """One property's value as Mnemo spans, or an empty list when it is unset."""
        kind = prop.get("type")
        value = prop.get(kind)

        if kind == "rich_text":
            return convert_rich_text(value, self.colors) if value else []
        if kind in {"number"}:
            return [plain(str(value))] if value is not None else []
        if kind in {"select", "status"}:
            return [plain(str((value or {}).get("name") or ""))] if value else []
        if kind == "multi_select":
            names = [str(item.get("name") or "") for item in (value or [])]
            return [plain(", ".join(n for n in names if n))] if names else []
        if kind == "date":
            if not value:
                return []
            start, end = value.get("start"), value.get("end")
            return [plain(f"{start} - {end}" if end else str(start or ""))]
        if kind == "checkbox":
            return [plain("✓" if value else "✗")]
        if kind in {"url", "email", "phone_number"}:
            if not value:
                return []
            href = str(value)
            if kind == "email":
                href = f"mailto:{value}"
            elif kind == "phone_number":
                href = f"tel:{value}"
            return [TextSpan(text=str(value), style=TextStyle(link_url=href))]
        if kind in {"people", "created_by", "last_edited_by"}:
            people = value if isinstance(value, list) else [value]
            names = [str((p or {}).get("name") or "") for p in people if p]
            return [plain(", ".join(n for n in names if n))] if any(names) else []
        if kind == "files":
            names = [str(item.get("name") or "") for item in (value or [])]
            return [plain(", ".join(n for n in names if n))] if names else []
        if kind in {"created_time", "last_edited_time"}:
            return [plain(str(value))] if value else []
        if kind == "formula":
            inner = (value or {}).get((value or {}).get("type") or "", None)
            return [plain(str(inner))] if inner not in (None, "") else []
        if kind == "rollup":
            rollup_type = (value or {}).get("type")
            inner = (value or {}).get(rollup_type or "")
            if isinstance(inner, list):
                return [plain(f"{len(inner)} item(s)")] if inner else []
            return [plain(str(inner))] if inner not in (None, "") else []
        if kind == "relation":
            return [plain(f"{len(value)} linked page(s)")] if value else []
        if kind == "unique_id":
            prefix = (value or {}).get("prefix") or ""
            number = (value or {}).get("number")
            return [plain(f"{prefix}-{number}" if prefix else str(number))] if number is not None else []
        if kind == "verification":
            state = (value or {}).get("state")
            return [plain(str(state))] if state else []
        if value in (None, "", [], {}):
            return []
        return [plain(str(value))]


def _tags(page: dict[str, Any]) -> list[str]:
    """
    Label-shaped properties become Mnemo tags.

    Select, multi-select and status are the three Notion property types whose
    whole purpose is categorisation, which is exactly what a Mnemo tag is. They
    are also still rendered in the property table, deliberately: the tag makes the
    note findable, the table row keeps it readable in context.
    """
    tags: list[str] = []
    for prop in (page.get("properties") or {}).values():
        if not isinstance(prop, dict):
            continue
        kind = prop.get("type")
        if kind in {"select", "status"}:
            name = (prop.get(kind) or {}).get("name")
            if name:
                tags.append(str(name))
        elif kind == "multi_select":
            tags.extend(str(item.get("name")) for item in (prop.get(kind) or []) if item.get("name"))
    # Stable order, no duplicates - a tag list that reshuffles between runs makes
    # every note look changed.
    return sorted(dict.fromkeys(tags))


def _cover_url(page: dict[str, Any]) -> str:
    cover = page.get("cover") or {}
    kind = cover.get("type")
    if kind and isinstance(cover.get(kind), dict):
        return str(cover[kind].get("url") or "")
    return ""


def _is_trashed(node: dict[str, Any]) -> bool:
    return bool(node.get("in_trash") or node.get("archived"))


def normalize_id(value: str) -> str:
    """
    Accepts a bare id, a dashed id, or any Notion URL and returns the dashed id.

    Notion ids appear in three shapes in the wild - the API returns them dashed,
    URLs carry them bare, and users paste either - so every entry point
    normalises rather than each caller remembering to.
    """
    text = (value or "").strip()
    if "/" in text or "?" in text:
        text = text.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if "-" in text:
            text = text.rsplit("-", 1)[-1]
    text = text.replace("-", "")
    return _dashed(text) if len(text) == 32 else value.strip()


def _dashed(hex32: str) -> str:
    if len(hex32) != 32:
        return hex32
    return f"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:]}"
