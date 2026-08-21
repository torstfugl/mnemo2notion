"""
Writes a ``.mnemo`` package into Notion as real pages.

The mapping of *content* lives in ``reverse.py``; this module owns everything
that only exists because the Notion API is a remote, rate-limited, batched
surface with rules about what one request may carry:

* **A hundred blocks per append, two levels of nesting per request.** The
  writer streams a note's blocks and flushes in batches. Anything nested deeper
  rides a follow-up append against the created parent's id - which resets the
  nesting budget, so arbitrarily deep Mnemo trees land intact.

* **Tables and column lists are atomic.** Notion rejects a table without rows
  and a column list without two non-empty columns, so those children are
  embedded in the same request (the one place the two-level budget is spent),
  and only their *grandchildren* are deferred.

* **Sub-notes are pages, and pages always land at the bottom.** Notion offers
  no way to insert a child page at a block position; it appears where the
  content has reached so far. The writer therefore walks a note's stream in
  order and creates each referenced sub-note the moment its ``Page`` block
  comes up, which makes "at the bottom so far" and "at the block's position"
  the same place.

* **Folders don't exist in Notion.** A Mnemo folder becomes an ordinary page
  holding its notes as sub-pages, so the tree shape survives even though the
  concept doesn't.

* **Images are uploaded, not linked.** Package assets go through Notion's File
  Upload API at attach time (an upload id dies after an hour, so uploading
  early would be a bug, not an optimization). A remote URL is passed through
  as an external file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .assets import ALLOWED_EXTENSIONS
from .notion import NotionClient, NotionError
from .package import read_package
from .reverse import NotionNode, ReverseContext, note_to_nodes

#: Notion's cap on children per append request.
MAX_BATCH = 100

_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


@dataclass
class PushOptions:
    #: Upload package images through Notion's File Upload API. Off means image
    #: blocks degrade to placeholder text.
    upload_images: bool = True
    #: Convert at most this many notes (for a trial run).
    limit: int | None = None


@dataclass
class PushResult:
    pages_created: int = 0
    blocks_written: int = 0
    images_uploaded: int = 0
    warnings: list[str] = field(default_factory=list)
    #: Mnemo note id -> created Notion page id.
    page_ids: dict[str, str] = field(default_factory=dict)


class NotionWriter:
    def __init__(
        self,
        client: NotionClient,
        *,
        options: PushOptions | None = None,
        progress: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self.client = client
        self.options = options or PushOptions()
        self.progress = progress
        self.result = PushResult()
        self._assets: dict[str, bytes] = {}
        self._notes_by_id: dict[str, dict[str, Any]] = {}
        self._created: set[str] = set()

    # -- entry point ------------------------------------------------------

    def push_package(self, package_path: str, parent_page_id: str) -> PushResult:
        manifest, notes, folders, _asset_ids = read_package(package_path)
        self._assets = _read_assets(package_path)
        self._notes_by_id = {n["noteId"]: n for n in notes if n.get("noteId")}

        if self.options.limit is not None:
            notes = notes[: self.options.limit]

        # Folders become pages; children map by folderId.
        folder_pages: dict[str, str] = {}
        for folder in sorted(folders, key=lambda f: (f.get("order", 0), f.get("name", ""))):
            parent = folder_pages.get(folder.get("parentId") or "", parent_page_id)
            page = self.client.create_page(parent, folder.get("name") or "Folder")
            folder_pages[folder["folderId"]] = page["id"]
            self.result.pages_created += 1
            self.progress(f"Created folder page '{folder.get('name')}'")

        # Top-level notes only: sub-notes are created from their parent's Page
        # blocks so they land inside the right page at the right moment.
        top_level = [n for n in notes if not n.get("parentNoteId")]
        skipped_subnotes = [n for n in notes if n.get("parentNoteId")]

        for index, note in enumerate(top_level, start=1):
            target = folder_pages.get(note.get("folderId") or "", parent_page_id)
            self.progress(f"[{index}/{len(top_level)}] {note.get('title') or 'Untitled'}")
            self._write_note(note, target)

        # A sub-note whose parent never referenced it (or whose parent is
        # outside the limit) must still exist somewhere.
        for note in skipped_subnotes:
            if note["noteId"] in self._created:
                continue
            parent_notion = self.result.page_ids.get(note.get("parentNoteId") or "")
            target = parent_notion or folder_pages.get(note.get("folderId") or "", parent_page_id)
            self.result.warnings.append(
                f"sub-note '{note.get('title')}' was not referenced by its parent's content; "
                "appended at the end of the parent page"
            )
            self._write_note(note, target)

        return self.result

    # -- one note ---------------------------------------------------------

    def _write_note(self, note: dict[str, Any], parent_page_id: str) -> str | None:
        note_id = note.get("noteId") or ""
        if note_id in self._created:
            return self.result.page_ids.get(note_id)
        self._created.add(note_id)

        try:
            page = self.client.create_page(
                parent_page_id,
                note.get("title") or "Untitled",
                icon_emoji=note.get("emoji") or None,
            )
        except NotionError as exc:
            self.result.warnings.append(f"could not create page '{note.get('title')}': {exc}")
            return None

        page_id = page["id"]
        self.result.page_ids[note_id] = page_id
        self.result.pages_created += 1

        ctx = ReverseContext(resolve_image=self._resolve_image)
        nodes = note_to_nodes(note, ctx)
        self.result.warnings.extend(f"'{note.get('title')}': {w}" for w in ctx.warnings)

        try:
            self._write_nodes(page_id, nodes)
        except NotionError as exc:
            self.result.warnings.append(f"page '{note.get('title')}' was only partially written: {exc}")
        return page_id

    # -- block streaming --------------------------------------------------

    def _write_nodes(self, parent_id: str, nodes: list[NotionNode]) -> None:
        """
        Appends a node stream to one parent, in order.

        Batches up to 100; a ``note_ref`` flushes first so the sub-page is
        created at the point the content has reached, which is what keeps
        document order.
        """
        batch: list[dict[str, Any]] = []
        # (index into batch, node with children to append after creation)
        deferred: list[tuple[int, NotionNode]] = []

        def flush() -> None:
            nonlocal batch, deferred
            if not batch:
                return
            results = self.client.append_children(parent_id, batch)
            self.result.blocks_written += len(batch)
            for index, node in deferred:
                if index < len(results):
                    self._append_deferred(results[index]["id"], node)
            batch = []
            deferred = []

        for node in nodes:
            if node.note_ref is not None:
                flush()
                self._create_referenced_note(node.note_ref, parent_id)
                continue
            if node.block is None:
                # A structural node with no block of its own: splice its
                # children into the stream.
                flush()
                self._write_nodes(parent_id, node.children)
                continue

            serialized, has_deferred = self._serialize(node)
            batch.append(serialized)
            if has_deferred:
                deferred.append((len(batch) - 1, node))
            if len(batch) >= MAX_BATCH:
                flush()
        flush()

    def _create_referenced_note(self, note_ref: str, fallback_parent: str) -> None:
        note = self._notes_by_id.get(note_ref)
        if note is None:
            self.result.warnings.append(
                "a page block references a note that is not in the package; skipped"
            )
            return
        if note["noteId"] in self._created:
            # Already created (e.g. two Page blocks referencing one note); a
            # second copy would be worse than one missing embed.
            self.result.warnings.append(
                f"note '{note.get('title')}' is embedded more than once; only the first became a page"
            )
            return
        # The page becomes a child of the page currently being written, not of
        # the note's folder: an embedded page is content, and content lives
        # where it is embedded.
        self._write_note(note, fallback_parent)

    # -- serialization within one request's nesting budget ----------------

    def _serialize(self, node: NotionNode) -> tuple[dict[str, Any], bool]:
        """
        One node as a request-ready dict, plus whether children were deferred.

        Plain children always defer (an append to the created id costs one more
        request but works at any depth). Inline children embed, because their
        parents are invalid without them - and only *their* plain grandchildren
        defer in turn.
        """
        block = dict(node.block or {})
        if not node.inline_children:
            return block, bool(node.children)

        kind = block.get("type")
        if kind == "table":
            # Rows are rich text all the way down: nothing ever defers.
            block["table"] = dict(block["table"])
            block["table"]["children"] = [dict(row.block or {}) for row in node.children]
            return block, False

        if kind == "column_list":
            columns = []
            any_deferred = False
            for column_node in node.children:
                column_block = dict(column_node.block or {})
                embedded = []
                for child in column_node.children:
                    child_block, child_deferred = self._embed_column_child(child)
                    embedded.append(child_block)
                    any_deferred = any_deferred or child_deferred
                column_block["column"] = dict(column_block.get("column") or {})
                column_block["column"]["children"] = embedded
                columns.append(column_block)
            block["column_list"] = {"children": columns}
            return block, any_deferred

        # A structural kind this writer doesn't know; treat children as deferred.
        return block, bool(node.children)

    def _embed_column_child(self, child: NotionNode) -> tuple[dict[str, Any], bool]:
        """
        A column's direct child, embedded at nesting level three.

        A table here still embeds its rows (level four - Notion accepts it for
        the same reason it demands them at all). Deeper structure defers.
        """
        if child.inline_children and child.block and child.block.get("type") == "table":
            serialized, _ = self._serialize(child)
            return serialized, False
        if child.inline_children:
            # A nested column split inside a column: Notion cannot create this
            # in one request. It defers wholesale via a placeholder the append
            # then replaces... which Notion also cannot do. Degrade: splice the
            # nested split's contents in sequence instead.
            flattened = {"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}
            return flattened, True
        return dict(child.block or {}), bool(child.children)

    def _append_deferred(self, created_id: str, node: NotionNode) -> None:
        """Writes the children a batch item could not carry, now that it has an id."""
        if not node.inline_children:
            self._write_nodes(created_id, node.children)
            return
        if node.block and node.block.get("type") == "column_list":
            # The columns were created inline; find their ids to give each its
            # deferred children. Fresh read: the cache must not answer for a
            # block created a second ago.
            columns = self.client.block_children_fresh(created_id)
            for column_node, created_column in zip(node.children, columns):
                if not any(child.children or child.inline_children for child in column_node.children):
                    continue
                # One fresh read per column that needs it; matched by position,
                # not equality - two identical paragraphs must not collapse.
                created_children = self.client.block_children_fresh(created_column["id"])
                for index, child in enumerate(column_node.children):
                    if child.inline_children:
                        # The flattened nested split: write its real content now,
                        # appended to the column.
                        self._write_nodes(created_column["id"], [child])
                    elif child.children and index < len(created_children):
                        self._write_nodes(created_children[index]["id"], child.children)

    # -- images -----------------------------------------------------------

    def _resolve_image(self, path: str) -> dict[str, Any] | None:
        if path.startswith(("http://", "https://")):
            return {"type": "external", "external": {"url": path}}
        if not self.options.upload_images:
            return None
        data = self._assets.get(path)
        if data is None:
            return None
        extension = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if extension not in ALLOWED_EXTENSIONS:
            return None
        try:
            upload_id = self.client.upload_file(
                path, _CONTENT_TYPES.get(extension, "application/octet-stream"), data
            )
        except NotionError as exc:
            self.result.warnings.append(f"image upload failed: {exc}")
            return None
        self.result.images_uploaded += 1
        return {"type": "file_upload", "file_upload": {"id": upload_id}}


def _read_assets(package_path: str) -> dict[str, bytes]:
    """The package's bundled images, keyed by asset id."""
    import zipfile

    from .package import ASSET_PREFIX, PAYLOAD_ROOT

    prefix = f"{PAYLOAD_ROOT}/{ASSET_PREFIX}"
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(package_path) as archive:
        for name in archive.namelist():
            if name.startswith(prefix):
                out[name[len(prefix) :]] = archive.read(name)
    return out
