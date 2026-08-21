"""The .mnemo package: layout, manifest, and the SQLite payload."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from notion2mnemo import mnemo
from notion2mnemo.mnemo import Block, Folder, Note, plain
from notion2mnemo.package import PAYLOAD_ROOT, read_package, write_package

#: Fixed, because a note's timestamps come from Notion in real use. Letting them
#: default to the wall clock would make the reproducibility test measure the
#: clock rather than the writer.
FIXED_TIME = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)


def sample_note() -> Note:
    return Note(
        note_id=mnemo.stable_id("note", "page-1"),
        title="Physics",
        emoji="⚛️",
        tags=["term-1"],
        created_at=FIXED_TIME,
        modified_at=FIXED_TIME,
        blocks=[
            Block(type=mnemo.HEADING1, spans=[plain("Kinematics")],
                  id=mnemo.stable_id("block", "b1")),
            Block(type=mnemo.EQUATION, payload=mnemo.equation_payload("v = v_0 + at"),
                  id=mnemo.stable_id("block", "b2")),
            Block(type=mnemo.IMAGE, spans=[plain("Graph")],
                  payload=mnemo.image_payload("a" * 32 + ".png", alt="Graph"),
                  id=mnemo.stable_id("block", "b3")),
        ],
    )


class PackageLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "out.mnemo"

    def write(self, notes=None, folders=None, assets=None):
        return write_package(
            self.path,
            notes if notes is not None else [sample_note()],
            folders if folders is not None else [Folder(folder_id="f1", name="Notion")],
            assets if assets is not None else {"a" * 32 + ".png": b"\x89PNG\r\n\x1a\n"},
            app_version="test",
        )

    def test_archive_entry_paths_match_what_import_looks_for(self):
        # MnemoPackageService selects a payload's files by the manifest's `path`
        # prefix, and the notes handler restores anything under
        # `assets/note-assets/` to Mnemo's asset directory.
        self.write()
        with zipfile.ZipFile(self.path) as archive:
            names = set(archive.namelist())
        self.assertIn("manifest.json", names)
        self.assertIn(f"{PAYLOAD_ROOT}/notes.db", names)
        self.assertIn(f"{PAYLOAD_ROOT}/assets/note-assets/{'a' * 32}.png", names)

    def test_no_entry_path_would_be_rejected_as_unsafe(self):
        self.write()
        with zipfile.ZipFile(self.path) as archive:
            for name in archive.namelist():
                self.assertFalse(name.startswith("/"), name)
                self.assertNotIn("../", name)
                self.assertLessEqual(len([p for p in name.split("/") if p]), 32, name)

    def test_manifest_shape(self):
        self.write()
        with zipfile.ZipFile(self.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["format"], "mnemo-package")
        self.assertEqual(manifest["version"], 1)
        entry = manifest["entries"][0]
        self.assertEqual(entry["payloadType"], "notes")
        self.assertEqual(entry["path"], PAYLOAD_ROOT)
        self.assertEqual(entry["itemCount"], 1)
        self.assertEqual(entry["schemaVersion"], 1)

    def test_round_trip_through_the_sqlite_payload(self):
        self.write()
        manifest, notes, folders, assets = read_package(self.path)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["title"], "Physics")
        self.assertEqual(notes[0]["emoji"], "⚛️")
        self.assertEqual(notes[0]["tags"], ["term-1"])
        self.assertEqual(folders[0]["name"], "Notion")
        self.assertEqual(assets, ["a" * 32 + ".png"])

    def test_blocks_survive_the_json_round_trip(self):
        self.write()
        _, notes, _, _ = read_package(self.path)
        blocks = notes[0]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["Heading1", "Equation", "Image"])
        self.assertEqual(blocks[1]["payload"]["latex"], "v = v_0 + at")
        self.assertEqual(blocks[2]["payload"]["path"], "a" * 32 + ".png")
        self.assertEqual([b["order"] for b in blocks], [0, 1, 2])

    def test_two_runs_over_the_same_content_produce_identical_bytes(self):
        # A fixed ZIP timestamp and a stable id scheme mean a diff between two
        # exports is a real content change, not noise.
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = Path(self.tmp.name) / "a.mnemo"
        second = Path(self.tmp.name) / "b.mnemo"
        for target in (first, second):
            write_package(
                target,
                [sample_note()],
                [Folder(folder_id="f1", name="Notion")],
                {"a" * 32 + ".png": b"\x89PNG"},
                app_version="test",
                created_at=stamp,
            )
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_empty_asset_set_is_valid(self):
        self.write(assets={})
        manifest, notes, _, assets = read_package(self.path)
        self.assertEqual(assets, [])
        self.assertEqual(len(notes), 1)


if __name__ == "__main__":
    unittest.main()
