"""
A small Notion API client, shaped by what a bulk export actually needs.

Three things matter here and nothing else does:

* **Rate limits.** Notion allows roughly three requests per second and answers a
  burst with 429. A full workspace is thousands of requests (one per block page,
  per level of nesting), so the throttle is not optional and the client sleeps
  for ``Retry-After`` rather than guessing.

* **A disk cache.** The first run over a large workspace takes a while, and the
  second run should not. Every GET/POST response is cached by request identity,
  so re-running after fixing a colour map costs no API calls at all. It also
  makes the converter debuggable offline.

* **Signed file URLs expire.** Notion serves uploaded images from S3 links that
  die after an hour, so image bytes are fetched during the walk (see
  ``assets.py``) and never from a cached URL on a later run. The cache stores the
  block JSON; the *files* are stored separately, keyed by content.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterator

import requests

API_ROOT = "https://api.notion.com/v1"

#: The version this tool is written against. It is still supported and covers
#: every page and single-source database. Pass ``--notion-version 2025-09-03``
#: (or later) for a workspace with multi-source databases; ``query_database``
#: handles both shapes.
DEFAULT_VERSION = "2022-06-28"

#: The version at which databases split into databases-plus-data-sources.
DATA_SOURCE_VERSION = "2025-09-03"


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        version: str = DEFAULT_VERSION,
        cache_dir: Path | None = None,
        requests_per_second: float = 2.5,
        max_retries: int = 5,
        timeout: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.version = version
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._max_retries = max_retries
        self._timeout = timeout
        self._last_call = 0.0
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            }
        )
        self.request_count = 0
        self.cache_hits = 0

    # -- plumbing ---------------------------------------------------------

    def _cache_path(self, method: str, path: str, body: dict[str, Any] | None) -> Path | None:
        if not self.cache_dir:
            return None
        key = json.dumps([self.version, method, path, body], sort_keys=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{digest}.json"

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        cache_path = self._cache_path(method, path, body)
        if cache_path is not None and cache_path.exists():
            self.cache_hits += 1
            return json.loads(cache_path.read_text(encoding="utf-8"))

        url = f"{API_ROOT}{path}"
        last_error: str = ""
        for attempt in range(self._max_retries):
            self._throttle()
            self.request_count += 1
            try:
                response = self._session.request(
                    method, url, json=body, timeout=self._timeout
                )
            except requests.RequestException as exc:  # network flake
                last_error = str(exc)
                time.sleep(min(2**attempt, 30))
                continue

            if response.status_code == 429:
                # Notion tells us how long to wait; obeying it is strictly better
                # than a backoff curve, which either wastes time or gets throttled
                # again on the next attempt.
                delay = float(response.headers.get("Retry-After", "1") or 1)
                time.sleep(min(delay, 60))
                continue

            if response.status_code >= 500:
                last_error = f"{response.status_code} {response.text[:200]}"
                time.sleep(min(2**attempt, 30))
                continue

            if response.status_code >= 400:
                raise NotionError(
                    f"{method} {path} failed: {response.status_code} {response.text[:400]}"
                )

            data = response.json()
            if cache_path is not None:
                cache_path.write_text(json.dumps(data), encoding="utf-8")
            return data

        raise NotionError(f"{method} {path} failed after {self._max_retries} attempts: {last_error}")

    def _paginate(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            if method == "GET":
                suffix = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
                data = self.request("GET", path + suffix)
            else:
                payload = dict(body or {})
                payload["page_size"] = 100
                if cursor:
                    payload["start_cursor"] = cursor
                data = self.request(method, path, payload)

            yield from data.get("results", [])
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor:
                return

    # -- endpoints --------------------------------------------------------

    def search_pages(self, query: str = "") -> list[dict[str, Any]]:
        """Every page the integration can see. Databases are fetched separately."""
        body: dict[str, Any] = {"filter": {"property": "object", "value": "page"}}
        if query:
            body["query"] = query
        return list(self._paginate("POST", "/search", body))

    def search_databases(self, query: str = "") -> list[dict[str, Any]]:
        body: dict[str, Any] = {"filter": {"property": "object", "value": "database"}}
        if query:
            body["query"] = query
        return list(self._paginate("POST", "/search", body))

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self.request("GET", f"/pages/{page_id}")

    def get_database(self, database_id: str) -> dict[str, Any]:
        return self.request("GET", f"/databases/{database_id}")

    def get_block(self, block_id: str) -> dict[str, Any]:
        return self.request("GET", f"/blocks/{block_id}")

    def block_children(self, block_id: str) -> list[dict[str, Any]]:
        return list(self._paginate("GET", f"/blocks/{block_id}/children"))

    def block_children_fresh(self, block_id: str) -> list[dict[str, Any]]:
        """
        Children read past the cache, for blocks this run just created.

        The cache answers by request identity, so after a write it would happily
        return the state from before the write - correct for the export path,
        poison for the import one.
        """
        cache_dir, self.cache_dir = self.cache_dir, None
        try:
            return list(self._paginate("GET", f"/blocks/{block_id}/children"))
        finally:
            self.cache_dir = cache_dir

    def query_database(self, database_id: str) -> list[dict[str, Any]]:
        """
        Every row of a database, on either side of the 2025-09-03 split.

        Before that version a database *is* the queryable thing. From it on, a
        database is a container of data sources and the query moved to
        ``/data_sources/{id}/query``. Both shapes are handled here so the same
        converter works against an old integration and a new one.
        """
        if self.version >= DATA_SOURCE_VERSION:
            database = self.get_database(database_id)
            sources = database.get("data_sources") or []
            if sources:
                rows: list[dict[str, Any]] = []
                for source in sources:
                    source_id = source.get("id")
                    if source_id:
                        rows.extend(self._paginate("POST", f"/data_sources/{source_id}/query", {}))
                return rows
        return list(self._paginate("POST", f"/databases/{database_id}/query", {}))

    # -- write endpoints (Mnemo -> Notion) --------------------------------
    #
    # Writes are never cached: the cache exists so a re-run of a *read* is
    # free, and replaying a cached "created page" as if it happened again is
    # exactly the corruption a cache must never cause.

    def _request_uncached(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        cache_dir, self.cache_dir = self.cache_dir, None
        try:
            return self.request(method, path, body)
        finally:
            self.cache_dir = cache_dir

    def create_page(
        self,
        parent_page_id: str,
        title: str,
        *,
        icon_emoji: str | None = None,
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": title[:2000]}}]}
            },
        }
        if icon_emoji:
            body["icon"] = {"type": "emoji", "emoji": icon_emoji}
        if children:
            body["children"] = children
        return self._request_uncached("POST", "/pages", body)

    def append_children(self, block_id: str, children: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Appends up to 100 blocks and returns the created blocks, in order."""
        data = self._request_uncached("PATCH", f"/blocks/{block_id}/children", {"children": children})
        return data.get("results", [])

    def upload_file(self, filename: str, content_type: str, data: bytes) -> str:
        """
        Uploads one file through Notion's File Upload API and returns the upload id.

        Two steps: create the upload object, then send the bytes to its send
        endpoint as multipart. The id must be attached to a block within an
        hour or Notion archives it, so callers upload close to the attach.
        """
        created = self._request_uncached(
            "POST", "/file_uploads", {"filename": filename, "content_type": content_type}
        )
        upload_id = created["id"]

        self._throttle()
        self.request_count += 1
        response = self._session.post(
            f"{API_ROOT}/file_uploads/{upload_id}/send",
            files={"file": (filename, data, content_type)},
            # requests must set the multipart boundary itself; the session-level
            # application/json would corrupt the body.
            headers={"Content-Type": None},
            timeout=self._timeout,
        )
        if response.status_code >= 400:
            raise NotionError(
                f"file upload '{filename}' failed: {response.status_code} {response.text[:300]}"
            )
        return upload_id

    def download(self, url: str) -> tuple[bytes, str]:
        """
        Fetches a file, returning its bytes and the server's content type.

        Deliberately not cached and not throttled: these are S3 URLs, not API
        calls, they do not count against the rate limit, and the signature
        expires within the hour, so a cached response would be a cached 403.

        The session's auth and content-type headers are stripped rather than
        overridden - S3 rejects a request carrying a bearer token it did not
        issue, and ``None`` is how requests removes a session-level header for
        one call.
        """
        response = self._session.get(
            url,
            timeout=self._timeout,
            headers={"Authorization": None, "Notion-Version": None, "Content-Type": None},
        )
        response.raise_for_status()
        return response.content, response.headers.get("Content-Type", "")
