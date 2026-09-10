"""The files module: own files *and* the files of every group.

Two ways in, in this order of preference:

``webdav`` (default)
    ``https://<domain>/webdav/`` exposes both areas as a normal WebDAV tree:
    ``Files/`` is "Dateien → Eigene", ``Groups/<Gruppe>/`` is
    "Dateien → Gruppen". Complete metadata, real downloads, uploads.

``web``
    The browser file manager under ``/iserv/file/``. Used when a school has
    WebDAV switched off. Downloads go through ``/iserv/fs/file/<pfad>``, the
    same links the exercise module hands out for attachments.
"""

from __future__ import annotations

import logging
import posixpath
import re
from datetime import datetime, timedelta
from typing import Iterable, Iterator
from urllib.parse import unquote

from .exceptions import IServAPIError, IServError, IServModuleUnavailable
from .models import FileEntry, FileScope, Group
from .parsing import extract_php_data, parse_datetime, parse_size, text_of
from .webdav import WebDAVClient

log = logging.getLogger(__name__)

OWN_ROOT = "Files"
GROUP_ROOT = "Groups"


class FilesModule:
    """Browse, search, download and upload IServ files."""

    def __init__(self, client, *, webdav_url: str | None = None) -> None:
        self.client = client
        self._webdav_url = webdav_url or f"https://{client.domain}/webdav/"
        self._webdav: WebDAVClient | None = None
        self._backend: str | None = None

    # ------------------------------------------------------------------

    @property
    def webdav(self) -> WebDAVClient:
        if self._webdav is None:
            self._webdav = WebDAVClient(
                self._webdav_url,
                self.client.username,
                self.client._password,
                timeout=self.client.transport.timeout,
                verify=self.client.transport.verify,
            )
        return self._webdav

    @property
    def backend(self) -> str:
        """``webdav`` if the server answers PROPFIND, otherwise ``web``."""
        if self._backend is None:
            try:
                self.webdav.list("")
                self._backend = "webdav"
            except IServError as exc:
                log.info("WebDAV unavailable (%s) — falling back to the file manager", exc)
                self._backend = "web"
        return self._backend

    def use(self, backend: str) -> None:
        if backend not in {"webdav", "web", "auto"}:
            raise ValueError("backend must be 'webdav', 'web' or 'auto'")
        self._backend = None if backend == "auto" else backend

    # ------------------------------------------------------------------
    # browsing
    # ------------------------------------------------------------------

    def browse(self, path: str = "") -> list[FileEntry]:
        """List one directory of the file tree (``""`` is the root)."""
        key = f"files:{self.backend}:{path}"
        cached = self.client.cache.get(key)
        if cached is not None:
            return cached

        entries = (
            self.webdav.list(path) if self.backend == "webdav" else self._browse_web(path)
        )
        self.client.cache.set(key, entries)
        return entries

    def own(self, path: str = "") -> list[FileEntry]:
        """"Dateien → Eigene"."""
        return self.browse(posixpath.join(OWN_ROOT, path.strip("/")) if path else OWN_ROOT)

    def group_files(self, group: str, path: str = "") -> list[FileEntry]:
        """Files shared inside one group ("Dateien → Gruppen")."""
        base = posixpath.join(GROUP_ROOT, group.strip("/"))
        return self.browse(posixpath.join(base, path.strip("/")) if path else base)

    def groups(self) -> list[Group]:
        """Every group that has a file folder — i.e. every group of this user."""
        key = "files:groups"
        cached = self.client.cache.get(key)
        if cached is not None:
            return cached

        groups: list[Group] = []
        try:
            for entry in self.browse(GROUP_ROOT):
                if entry.is_dir:
                    groups.append(
                        Group(name=entry.name, act=entry.name, files_path=entry.path)
                    )
        except IServError as exc:
            log.info("Could not list group folders (%s); trying the profile page", exc)
            groups = self._groups_from_profile()

        self.client.cache.set(key, groups, ttl=3600)
        return groups

    def _groups_from_profile(self) -> list[Group]:
        """Fallback: the group-request form lists every group by name."""
        try:
            document = self.client.transport.html(
                "iserv/profile/grouprequest/add", optional=True
            )
        except IServError:
            return []
        groups: list[Group] = []
        for option in document.find_all("option"):
            name = text_of(option)
            if name and option.get("value"):
                groups.append(Group(name=name, act=option["value"]))
        return groups

    # ------------------------------------------------------------------

    def walk(self, path: str = "", *, max_depth: int = 5) -> Iterator[FileEntry]:
        """Yield every entry below ``path``."""
        if self.backend == "webdav":
            yield from self.webdav.walk(path, max_depth=max_depth)
            return

        queue: list[tuple[str, int]] = [(path, 0)]
        while queue:
            current, depth = queue.pop(0)
            try:
                entries = self.browse(current)
            except IServError as exc:
                log.debug("Skipping %s: %s", current, exc)
                continue
            for entry in entries:
                yield entry
                if entry.is_dir and depth + 1 < max_depth:
                    queue.append((entry.path, depth + 1))

    def search(self, query: str, path: str = "", *, max_depth: int = 5,
               limit: int = 200, extensions: Iterable[str] | None = None) -> list[FileEntry]:
        """Find files whose name contains ``query`` (case-insensitive)."""
        needle = query.lower()
        wanted = {e.lower().lstrip(".") for e in extensions} if extensions else None
        found: list[FileEntry] = []
        for entry in self.walk(path, max_depth=max_depth):
            if entry.is_dir or needle not in entry.name.lower():
                continue
            if wanted and entry.extension not in wanted:
                continue
            found.append(entry)
            if len(found) >= limit:
                break
        return found

    def recent(self, *, days: int = 7, path: str = GROUP_ROOT, max_depth: int = 4,
               limit: int = 50) -> list[FileEntry]:
        """Recently changed files — "what is new in my group folders?"."""
        cutoff = datetime.now() - timedelta(days=days)
        recent: list[FileEntry] = []
        for entry in self.walk(path, max_depth=max_depth):
            if entry.is_dir or entry.modified is None:
                continue
            modified = entry.modified.replace(tzinfo=None)
            if modified >= cutoff:
                recent.append(entry)
        recent.sort(key=lambda e: e.modified or datetime.min, reverse=True)
        return recent[:limit]

    # ------------------------------------------------------------------
    # transfers
    # ------------------------------------------------------------------

    def download(self, path: str) -> bytes:
        if self.backend == "webdav":
            return self.webdav.download(path)
        return self.client.transport.download(self._web_download_url(path))

    def download_to(self, path: str, destination: str) -> str:
        if self.backend == "webdav":
            return self.webdav.download_to(path, destination)
        with open(destination, "wb") as handle:
            handle.write(self.download(path))
        return destination

    def upload(self, remote_path: str, data: bytes | str) -> None:
        self._require_webdav("Uploading")
        self.webdav.upload(remote_path, data)
        self.client.cache.invalidate("files:")

    def upload_file(self, local_path: str, remote_path: str) -> None:
        self._require_webdav("Uploading")
        self.webdav.upload_file(local_path, remote_path)
        self.client.cache.invalidate("files:")

    def mkdir(self, path: str) -> None:
        self._require_webdav("Creating folders")
        self.webdav.mkdir(path)
        self.client.cache.invalidate("files:")

    def delete(self, path: str) -> None:
        self._require_webdav("Deleting")
        self.webdav.delete(path)
        self.client.cache.invalidate("files:")

    def _require_webdav(self, action: str) -> None:
        if self.backend != "webdav":
            raise IServModuleUnavailable(f"{action} requires WebDAV, which this server refused")

    # ------------------------------------------------------------------
    # quota
    # ------------------------------------------------------------------

    def disk_usage(self) -> dict[str, int | None]:
        """Quota of the personal file area (``/iserv/du/account``)."""
        response = self.client.transport.request("GET", "iserv/du/account")
        if response.status_code >= 400:
            raise IServModuleUnavailable(
                "Quota page unavailable", status=response.status_code, url=response.url
            )
        payload = extract_php_data(response.text, "user-diskusage-data")
        if isinstance(payload, dict):
            used = payload.get("used") or payload.get("usage")
            total = payload.get("total") or payload.get("quota") or payload.get("max")
            return {"used": parse_size(used), "total": parse_size(total)}
        return {"used": None, "total": None}

    def folder_size(self, path: str) -> dict:
        """``/iserv/file/calc?path=…`` — the size of one folder."""
        return self.client.transport.get_json(
            "iserv/file/calc", params={"path": path}, optional=True
        ) or {}

    # ------------------------------------------------------------------
    # web fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _web_download_url(path: str) -> str:
        return "iserv/fs/file/" + path.lstrip("/")

    def _browse_web(self, path: str) -> list[FileEntry]:
        """Scrape the file manager listing at ``/iserv/file/<pfad>``.

        Best effort: the markup differs between IServ versions, so entries are
        recognised by their links (``/iserv/fs/file/…`` for files,
        ``/iserv/file/…`` for folders) rather than by fragile CSS classes.
        """
        page = "iserv/file/" + path.lstrip("/") if path else "iserv/file/"
        try:
            document = self.client.transport.html(page, optional=True)
        except IServAPIError as exc:
            raise IServModuleUnavailable(
                f"The file manager did not answer for {path!r}: {exc}"
            ) from exc

        entries: list[FileEntry] = []
        seen: set[str] = set()

        for link in document.find_all("a", href=True):
            href = link["href"]
            name = text_of(link)
            if not name:
                continue

            if "/iserv/fs/file/" in href:
                entry_path = unquote(href.split("/iserv/fs/file/", 1)[1])
                is_dir = False
            elif re.search(r"/iserv/file/(?!.*(?:calc|search)).+", href):
                entry_path = unquote(href.split("/iserv/file/", 1)[1]).rstrip("/")
                is_dir = True
            else:
                continue

            if not entry_path or entry_path in seen or entry_path == path.strip("/"):
                continue
            seen.add(entry_path)

            row = link.find_parent("tr")
            cells = [text_of(td) for td in row.find_all("td")] if row else []
            size = next((s for cell in cells if (s := parse_size(cell)) not in (None, 0)), None)
            modified = next(
                (m for cell in cells if (m := parse_datetime(cell)) is not None), None
            )
            scope = (
                FileScope.GROUP if entry_path.split("/")[0].lower() in {"groups", "gruppen"}
                else FileScope.OWN if entry_path.split("/")[0].lower() in {"files", "eigene"}
                else FileScope.UNKNOWN
            )
            entries.append(
                FileEntry(
                    name=posixpath.basename(entry_path) or name,
                    path=entry_path,
                    is_dir=is_dir,
                    size=None if is_dir else size,
                    modified=modified,
                    scope=scope,
                    group=entry_path.split("/")[1] if scope is FileScope.GROUP and "/" in entry_path else None,
                    download_url=None if is_dir else f"https://{self.client.domain}/{self._web_download_url(entry_path)}",
                )
            )

        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def close(self) -> None:
        if self._webdav is not None:
            self._webdav.close()
