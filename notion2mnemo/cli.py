"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .assets import AssetStore
from .colors import load_overrides
from .notion import DEFAULT_VERSION, NotionClient, NotionError
from .package import read_package, write_package
from .walker import WalkOptions, Walker

EPILOG = """\
setup:
  1. Create an internal integration at https://www.notion.so/my-integrations
     and copy its secret.
  2. In Notion, open each top-level page you want to move and use
     ... -> Connections -> add your integration. Sub-pages are included
     automatically.
  3. export NOTION_TOKEN=ntn_...        (Windows: $env:NOTION_TOKEN = "ntn_...")

examples:
  python -m notion2mnemo -o notes.mnemo
  python -m notion2mnemo --page https://www.notion.so/My-Page-abc123... --covers
  python -m notion2mnemo --database 1234abcd... --db-properties none

  python -m notion2mnemo push notes.mnemo --parent https://www.notion.so/Imports-...
  python -m notion2mnemo gui

then, in Mnemo: Notes -> Import -> pick the .mnemo file.

subcommands:
  pull   Notion -> .mnemo (the default when no subcommand is given)
  push   .mnemo -> Notion (creates real pages under --parent)
  gui    open the graphical app
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notion2mnemo",
        description="Convert Notion pages into a Mnemo .mnemo notes package.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"notion2mnemo {__version__}")

    source = parser.add_argument_group("what to convert")
    source.add_argument(
        "--page",
        action="append",
        default=[],
        metavar="ID_OR_URL",
        help="convert this page and its sub-pages; repeatable. "
        "Omit --page and --database to convert everything the integration can see.",
    )
    source.add_argument(
        "--database",
        action="append",
        default=[],
        metavar="ID_OR_URL",
        help="convert this database's rows into a folder of notes; repeatable.",
    )
    source.add_argument(
        "--no-databases",
        action="store_true",
        help="skip databases entirely when scanning the whole workspace.",
    )
    source.add_argument("--limit", type=int, metavar="N", help="stop after N pages (for a trial run).")

    output = parser.add_argument_group("output")
    output.add_argument(
        "-o",
        "--output",
        default="notion-export.mnemo",
        metavar="FILE",
        help="package to write (default: notion-export.mnemo).",
    )
    output.add_argument(
        "--folder",
        default="Notion",
        metavar="NAME",
        help='folder to import everything under (default: "Notion"). Use "" for the tree root.',
    )
    output.add_argument(
        "--covers",
        action="store_true",
        help="download page covers as well as inline images.",
    )
    output.add_argument(
        "--db-properties",
        choices=("table", "none"),
        default="table",
        help="how to carry a database row's properties (default: table at the top of the note). "
        "Select and multi-select always become tags either way.",
    )
    output.add_argument(
        "--color-map",
        metavar="FILE",
        help="JSON file overriding the Notion-colour to Mnemo-swatch mapping.",
    )
    output.add_argument(
        "--dry-run",
        action="store_true",
        help="convert and report, but write no package.",
    )

    api = parser.add_argument_group("api")
    api.add_argument(
        "--token",
        default=os.environ.get("NOTION_TOKEN", ""),
        help="Notion integration secret (default: $NOTION_TOKEN).",
    )
    api.add_argument(
        "--notion-version",
        default=DEFAULT_VERSION,
        metavar="YYYY-MM-DD",
        help=f"Notion API version (default: {DEFAULT_VERSION}). "
        "Use 2025-09-03 or later for databases with multiple data sources.",
    )
    api.add_argument(
        "--cache-dir",
        default=".notion-cache",
        metavar="DIR",
        help="where to cache API responses so re-runs are fast (default: .notion-cache).",
    )
    api.add_argument("--no-cache", action="store_true", help="do not cache API responses.")
    api.add_argument(
        "--rate",
        type=float,
        default=2.5,
        metavar="RPS",
        help="requests per second (default: 2.5; Notion's limit is about 3).",
    )

    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary.")
    return parser


def build_push_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notion2mnemo push",
        description="Write a .mnemo package into Notion as real pages.",
    )
    parser.add_argument("package", help="the .mnemo file to push")
    parser.add_argument(
        "--parent",
        required=True,
        metavar="ID_OR_URL",
        help="the Notion page the imported pages are created under. "
        "The integration must be connected to it.",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="skip uploading images (image blocks become placeholder text).",
    )
    parser.add_argument("--limit", type=int, metavar="N", help="push at most N notes.")
    parser.add_argument("--token", default=os.environ.get("NOTION_TOKEN", ""))
    parser.add_argument("--notion-version", default=DEFAULT_VERSION, metavar="YYYY-MM-DD")
    parser.add_argument("--rate", type=float, default=2.5, metavar="RPS")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def run_push(argv: list[str]) -> int:
    from .push import NotionWriter, PushOptions
    from .walker import normalize_id

    args = build_push_parser().parse_args(argv)
    if not args.token:
        print("No Notion token. Set NOTION_TOKEN or pass --token.", file=sys.stderr)
        return 2
    if not Path(args.package).exists():
        print(f"'{args.package}' does not exist.", file=sys.stderr)
        return 2

    # No cache: this run is all writes, and stale reads against just-created
    # blocks are the one thing the cache would add.
    client = NotionClient(args.token, version=args.notion_version, requests_per_second=args.rate)
    writer = NotionWriter(
        client,
        options=PushOptions(upload_images=not args.no_images, limit=args.limit),
        progress=(lambda m: None) if args.quiet else (lambda m: print(m, flush=True)),
    )
    try:
        result = writer.push_package(args.package, normalize_id(args.parent))
    except NotionError as exc:
        print(f"Notion API error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Pages already created remain in Notion.", file=sys.stderr)
        return 130

    print(
        f"\nCreated {result.pages_created} page(s), {result.blocks_written} block(s), "
        f"{result.images_uploaded} image(s) uploaded."
    )
    for warning in result.warnings:
        print(f"  warning: {warning}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Subcommands, with the bare invocation staying the original pull for
    # compatibility with every command line already written down.
    if argv and argv[0] == "push":
        return run_push(argv[1:])
    if argv and argv[0] == "gui":
        from .gui.app import run_gui

        return run_gui()
    if argv and argv[0] == "pull":
        argv = argv[1:]

    args = build_parser().parse_args(argv)

    if not args.token:
        print(
            "No Notion token. Set NOTION_TOKEN or pass --token.\n"
            "Create one at https://www.notion.so/my-integrations, then share your\n"
            "pages with it via ... -> Connections.",
            file=sys.stderr,
        )
        return 2

    def progress(message: str) -> None:
        if not args.quiet:
            print(message, flush=True)

    try:
        colors = load_overrides(args.color_map)
    except (OSError, ValueError) as exc:
        print(f"Could not read colour map: {exc}", file=sys.stderr)
        return 2

    client = NotionClient(
        args.token,
        version=args.notion_version,
        cache_dir=None if args.no_cache else Path(args.cache_dir),
        requests_per_second=args.rate,
    )
    assets = AssetStore(downloader=client.download)
    options = WalkOptions(
        root_folder=args.folder,
        database_properties=args.db_properties,
        include_databases=not args.no_databases,
        covers=args.covers,
        limit=args.limit,
    )

    walker = Walker(client, colors, assets, options, progress=progress)
    try:
        walker.discover(page_ids=args.page, database_ids=args.database)
        result = walker.convert()
    except NotionError as exc:
        print(f"Notion API error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run to resume from the cache.", file=sys.stderr)
        return 130

    if not result.notes:
        print(
            "No pages were converted. The most common cause is that the integration\n"
            "has not been added to any page: open a page in Notion, then\n"
            "... -> Connections -> your integration.",
            file=sys.stderr,
        )
        return 1

    _report(result, assets, client, quiet=args.quiet)

    if args.dry_run:
        progress("Dry run: no package written.")
        return 0

    output = write_package(
        args.output,
        result.notes,
        result.folders,
        assets.files,
        app_version=f"notion2mnemo {__version__}",
    )

    # Read it back the way Mnemo will, so a malformed package is caught here
    # rather than by an import that reports nothing at all.
    manifest, notes, folders, asset_ids = read_package(output)
    if manifest.get("format") != "mnemo-package" or len(notes) != len(result.notes):
        print("The package failed its own verification pass.", file=sys.stderr)
        return 1

    size_mb = output.stat().st_size / (1024 * 1024)
    print(
        f"\nWrote {output} ({size_mb:.1f} MB): "
        f"{len(notes)} note(s), {len(folders)} folder(s), {len(asset_ids)} image(s)."
    )

    if result.warnings:
        log = output.with_suffix(".warnings.txt")
        log.write_text("\n".join(result.warnings), encoding="utf-8")
        print(f"{len(result.warnings)} warning(s) written to {log}")

    print("\nImport it in Mnemo: Notes -> Import -> select the .mnemo file.")
    return 0


def _report(result, assets: AssetStore, client: NotionClient, *, quiet: bool) -> None:
    stats = result.stats
    print(
        f"\nConverted {len(result.notes)} note(s) into {len(result.folders)} folder(s): "
        f"{stats.blocks_out} block(s), {stats.images} image(s), {stats.equations} equation(s)."
    )
    if result.skipped:
        print(f"{result.skipped} page(s) failed to convert; see the warnings log.")
    if stats.dropped and not quiet:
        summary = ", ".join(f"{kind} x{count}" for kind, count in sorted(stats.dropped.items()))
        print(f"Notion blocks with no Mnemo equivalent, skipped: {summary}")
    if not quiet:
        print(
            f"API: {client.request_count} request(s), {client.cache_hits} served from cache."
        )
