"""
The desktop app: a WebView2 window whose page talks to this process.

Every conversion still runs through the same engine the CLI uses - the GUI is
a veneer, which is the property that keeps the two from drifting: a format fix
lands once and both surfaces get it.

Threading is the only real subtlety. pywebview dispatches JS-to-Python calls
on worker threads but a conversion can run for minutes, so the Api methods
that convert return immediately and push progress back into the page with
``evaluate_js``. One conversion at a time: the page disables its buttons, and
the Python side enforces it too, because the page is not a security boundary.

Stopping works the same way round. The engine has no cancel hook and does not
need one: it calls ``progress`` between pages, so raising from inside that
callback unwinds the run at a page boundary - the only place where stopping
leaves nothing half-written. ``_Cancelled`` derives from BaseException so that
the engine's broad ``except Exception`` guards cannot swallow it.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from .. import __version__
from ..assets import AssetStore
from ..colors import ColorMap
from ..notion import DEFAULT_VERSION, NotionClient, NotionError
from ..package import read_package, write_package
from ..push import NotionWriter, PushOptions
from ..walker import WalkOptions, Walker, database_title, normalize_id, page_title

APP_NAME = "Notion ↔ Mnemo Converter"

CONFIG_DIR = Path.home() / ".notion2mnemo"
CONFIG_PATH = CONFIG_DIR / "config.json"

#: Both engines announce per-item progress as "[3/12] Some title".
_COUNTED = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)$")


class _Cancelled(BaseException):
    """Raised inside the progress callback to unwind a run the user stopped."""


def _load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_config(config: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _result_title(item: dict[str, Any]) -> str:
    if item.get("object") == "database":
        return database_title(item)
    return page_title(item)


def _result_icon(item: dict[str, Any]) -> str:
    icon = item.get("icon") or {}
    return icon.get("emoji") or "" if icon.get("type") == "emoji" else ""


def _documents_dir() -> Path:
    candidate = Path.home() / "Documents"
    return candidate if candidate.is_dir() else Path.home()


class Api:
    """The bridge the page calls. One instance per window."""

    def __init__(self) -> None:
        self._window = None  # set by run_gui once the window exists
        self._busy = threading.Lock()
        self._cancel = threading.Event()
        self._maximized = False

    # -- plumbing ---------------------------------------------------------

    def _push_js(self, function: str, payload: Any) -> None:
        if self._window is not None:
            self._window.evaluate_js(f"{function}({json.dumps(payload)})")

    def _progress(self, message: str) -> None:
        """
        Engine progress, structured for the page and used as the stop point.

        The engine speaks in sentences; the working screen wants a fraction and
        a name. Parsing here rather than teaching the engine a richer protocol
        keeps the CLI - the other caller - exactly as it was.
        """
        if self._cancel.is_set():
            raise _Cancelled()
        match = _COUNTED.match(message)
        if match:
            index, total, label = match.groups()
            payload = {
                "text": message,
                "index": int(index),
                "total": int(total),
                "label": label,
            }
        else:
            payload = {"text": message, "index": None, "total": None, "label": None}
        self._push_js("appProgress", payload)

    def _client(self, token: str) -> NotionClient:
        # No disk cache in the GUI: an interactive user expects "Convert" to
        # reflect what Notion holds right now, and the surprise of stale pages
        # outweighs the second run's speed.
        return NotionClient(token, version=DEFAULT_VERSION, cache_dir=None)

    # -- window chrome ----------------------------------------------------
    #
    # The window is frameless so the page can draw its own title bar; these
    # are the three buttons that would otherwise come from the OS.

    def window_minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def window_toggle_maximize(self) -> None:
        if self._window is None:
            return
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized

    def window_close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    # -- settings ---------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        config = _load_config()
        return {
            "version": __version__,
            "token": config.get("token") or "",
            "rememberToken": bool(config.get("token")),
            "defaultOutput": str(_documents_dir() / "notion-export.mnemo"),
        }

    def remember_token(self, token: str, remember: bool) -> None:
        config = _load_config()
        if remember and token:
            config["token"] = token
        else:
            config.pop("token", None)
        _save_config(config)

    def open_url(self, url: str) -> None:
        if url.startswith(("https://", "http://")):
            webbrowser.open(url)

    # -- discovery --------------------------------------------------------

    def list_content(self, token: str) -> dict[str, Any]:
        """Everything the integration can see, for the page picker."""
        if not token.strip():
            return {"error": _explain("Paste your integration key first.")}
        client = self._client(token.strip())
        try:
            pages = client.search_pages()
            databases = client.search_databases()
        except NotionError as exc:
            return {"error": _explain_api_error(exc)}

        items = []
        for page in pages:
            if page.get("in_trash") or page.get("archived"):
                continue
            parent_type = (page.get("parent") or {}).get("type") or ""
            items.append(
                {
                    "id": page["id"],
                    "kind": "page",
                    "title": _result_title(page),
                    "emoji": _result_icon(page),
                    # A sub-page or database row is included automatically when
                    # its parent is selected; saying so in the list is what lets
                    # the user select only top-level things.
                    "nested": parent_type in {"page_id", "database_id", "data_source_id", "block_id"},
                }
            )
        for database in databases:
            if database.get("in_trash") or database.get("archived"):
                continue
            items.append(
                {
                    "id": database["id"],
                    "kind": "database",
                    "title": _result_title(database),
                    "emoji": _result_icon(database),
                    "nested": (database.get("parent") or {}).get("type") == "page_id",
                }
            )
        if not items:
            return {
                "error": _explain(
                    "This key works, but no pages are shared with it yet.",
                    checks=[
                        "In Notion, open a page you want to move and choose "
                        "**⋯ → Connections** → your integration.",
                        "Pages inside the ones you share come along automatically, "
                        "so sharing the top-level ones is enough.",
                    ],
                )
            }
        return {"items": items}

    # -- file dialogs -----------------------------------------------------

    def pick_output_path(self, suggested: str = "") -> str | None:
        import webview

        start = Path(suggested).parent if suggested else _documents_dir()
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=str(start),
            save_filename=Path(suggested).name or "notion-export.mnemo",
            file_types=("Mnemo package (*.mnemo)",),
        )
        return result if isinstance(result, str) else (result[0] if result else None)

    def pick_package(self) -> dict[str, Any] | None:
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, file_types=("Mnemo package (*.mnemo)",)
        )
        path = result[0] if result else None
        if not path:
            return None
        return self.inspect_package(path)

    def inspect_package(self, path: str) -> dict[str, Any]:
        try:
            _manifest, notes, folders, assets = read_package(path)
        except Exception as exc:
            return {
                "error": _explain(
                    "That file isn't a Mnemo package.",
                    checks=[
                        "Export one from Mnemo with **Notes → Export → Mnemo package**.",
                        f"The file was read as far as: {exc}",
                    ],
                )
            }
        return {
            "path": path,
            "notes": [
                {
                    "title": note.get("title") or "Untitled",
                    "emoji": note.get("emoji") or "",
                    "blocks": len(note.get("blocks") or []),
                    "sub": bool(note.get("parentNoteId")),
                }
                for note in notes
            ],
            "folders": len(folders),
            "images": len(assets),
        }

    def open_containing_folder(self, path: str) -> None:
        import subprocess

        target = Path(path)
        if target.exists() and sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(target)])

    # -- running ----------------------------------------------------------

    def cancel_run(self) -> None:
        """Ask the running conversion to stop at the next page boundary."""
        self._cancel.set()

    def start_pull(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._start(self._run_pull, params)

    def start_push(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._start(self._run_push, params)

    def _start(self, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            return {"error": _explain("A conversion is already running.")}
        self._cancel.clear()
        threading.Thread(target=target, args=(params,), daemon=True).start()
        return {"started": True}

    # -- Notion -> Mnemo --------------------------------------------------

    def _run_pull(self, params: dict[str, Any]) -> None:
        try:
            token = (params.get("token") or "").strip()
            output = params.get("output") or str(_documents_dir() / "notion-export.mnemo")
            client = self._client(token)
            assets = AssetStore(downloader=client.download)
            options = WalkOptions(
                root_folder=params.get("folder") if params.get("folder") is not None else "Notion",
                database_properties="table" if params.get("dbProperties", True) else "none",
                covers=bool(params.get("covers")),
                limit=int(params["limit"]) if params.get("limit") else None,
            )
            walker = Walker(client, ColorMap(), assets, options, progress=self._progress)
            walker.discover(
                page_ids=params.get("pageIds") or (),
                database_ids=params.get("databaseIds") or (),
            )
            result = walker.convert()
            if not result.notes:
                self._push_js(
                    "appDone",
                    {
                        "error": _explain(
                            "Nothing came across.",
                            checks=[
                                "The pages you picked may no longer be shared with the "
                                "integration. In Notion: **⋯ → Connections**.",
                            ],
                        )
                    },
                )
                return

            self._progress("Writing the package…")
            path = write_package(
                output, result.notes, result.folders, assets.files,
                app_version=f"notion2mnemo {__version__}",
            )
            size_mb = path.stat().st_size / (1024 * 1024)
            self._push_js(
                "appDone",
                {
                    "path": str(path),
                    "folder": options.root_folder or "",
                    "notes": len(result.notes),
                    "folders": len(result.folders),
                    "images": len(assets.files),
                    "sizeMb": round(size_mb, 1),
                    "warnings": result.warnings,
                },
            )
        except _Cancelled:
            self._push_js("appDone", {"cancelled": True})
        except NotionError as exc:
            self._push_js("appDone", {"error": _explain_api_error(exc)})
        except Exception as exc:  # a GUI must never die silently
            self._push_js("appDone", {"error": _explain_unexpected(exc)})
        finally:
            self._busy.release()

    # -- Mnemo -> Notion --------------------------------------------------

    def _run_push(self, params: dict[str, Any]) -> None:
        try:
            token = (params.get("token") or "").strip()
            package = params.get("package") or ""
            parent = normalize_id(params.get("parent") or "")
            client = self._client(token)
            writer = NotionWriter(
                client,
                options=PushOptions(upload_images=bool(params.get("uploadImages", True))),
                progress=self._progress,
            )
            result = writer.push_package(package, parent)
            self._push_js(
                "appDone",
                {
                    "pages": result.pages_created,
                    "blocks": result.blocks_written,
                    "images": result.images_uploaded,
                    "warnings": result.warnings,
                },
            )
        except _Cancelled:
            self._push_js("appDone", {"cancelled": True})
        except NotionError as exc:
            self._push_js("appDone", {"error": _explain_api_error(exc)})
        except Exception as exc:
            self._push_js("appDone", {"error": _explain_unexpected(exc)})
        finally:
            self._busy.release()


# -- error explanations ---------------------------------------------------
#
# The failure screen says what to do, never what threw. Each explanation is a
# headline, an optional reassurance, and the one or two things actually worth
# checking; the raw text rides along under "copy the technical details" so a
# bug report can still carry it.


def _explain(title: str, *, detail: str = "", checks: list[str] | None = None) -> dict[str, Any]:
    return {"title": title, "checks": checks or [], "detail": detail or title}


def _explain_api_error(exc: NotionError) -> dict[str, Any]:
    text = str(exc)
    if "401" in text:
        return _explain(
            "Notion didn't accept that key.",
            detail=text,
            checks=[
                "The key starts with **ntn_** and is copied whole. It's easy to miss "
                "the last few characters.",
                "The integration still exists in Notion. If you deleted and remade it, "
                "the old key stops working.",
            ],
        )
    if "404" in text:
        return _explain(
            "Notion couldn't find that page.",
            detail=text,
            checks=[
                "The page exists, but the integration isn't connected to it. Open it in "
                "Notion and choose **⋯ → Connections** → your integration.",
            ],
        )
    if "403" in text:
        return _explain(
            "The integration isn't allowed to do that.",
            detail=text,
            checks=[
                "In Notion, open your integration's settings and make sure it may "
                "**read** content — and **insert** content, to create pages.",
            ],
        )
    if "429" in text:
        return _explain(
            "Notion asked us to slow down.",
            detail=text,
            checks=["Give it a minute and try again — nothing was lost."],
        )
    return _explain("Notion returned an error.", detail=text, checks=[text[:300]])


def _explain_unexpected(exc: Exception) -> dict[str, Any]:
    import traceback

    return _explain(
        "Something went wrong on this side.",
        detail="".join(traceback.format_exception(exc)),
        checks=[f"{type(exc).__name__}: {exc}"],
    )


def _web_dir() -> Path:
    # PyInstaller unpacks data files under sys._MEIPASS; a source checkout
    # serves them from the package directory.
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "notion2mnemo" / "gui" / "web"  # type: ignore[attr-defined]
    return Path(__file__).parent / "web"


def run_gui() -> int:
    import os

    import webview

    # CI / packaging smoke test: boot the whole stack (WebView2, the bridge,
    # the page) without showing a window, prove the page initialised, and exit.
    smoke = os.environ.get("NOTION2MNEMO_SMOKE") == "1"

    api = Api()
    window = webview.create_window(
        APP_NAME,
        url=str(_web_dir() / "index.html"),
        js_api=api,
        width=900,
        height=700,
        min_size=(820, 620),
        # The page draws the title bar, so the OS must not. easy_drag off:
        # dragging is confined to the .pywebview-drag-region strip, otherwise
        # a press anywhere in the window would move it.
        frameless=True,
        easy_drag=False,
        background_color="#FFFFFF",
        hidden=smoke,
    )
    api._window = window

    if smoke:
        def probe(w) -> None:
            import time

            time.sleep(3)  # give the page time to load and call get_state
            ready = w.evaluate_js("typeof state === 'object' && typeof appDone === 'function'")
            print(f"SMOKE ready={ready}", flush=True)
            w.destroy()

        webview.start(probe, window)
    else:
        webview.start()
    return 0
