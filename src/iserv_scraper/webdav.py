"""A minimal WebDAV client for the IServ file module.

IServ exposes *both* file areas over WebDAV at ``https://<domain>/webdav/``:

``Files/``
    "Dateien → Eigene" — the user's own files.
``Groups/<Gruppenname>/``
    "Dateien → Gruppen" — every group the user is a member of.

WebDAV authenticates with HTTP Basic, not with the web session cookie, so this
client keeps its own :class:`requests.Session`. Implementing PROPFIND directly
(≈100 lines of ``xml.etree``) avoids pulling in a WebDAV dependency and keeps
the returned objects in the same :class:`~iserv_scraper.models.FileEntry`
shape as the rest of the package.
"""

from __future__ import annotations

import logging
import posixpath
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

import requests

from .exceptions import IServAPIError, IServAuthError, IServNetworkError, IServNotFound
from .models import FileEntry, FileScope
from .parsing import parse_datetime

log = logging.getLogger(__name__)

DAV_NS = "{DAV:}"

PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:displayname/>
    <D:getcontentlength/>
    <D:getlastmodified/>
    <D:getcontenttype/>
    <D:getetag/>
    <D:resourcetype/>
  </D:prop>
</D:propfind>
"""


def _scope_of(path: str) -> tuple[FileScope, str | None]:
    """Derive ("own"/"group", group name) from a WebDAV path."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return FileScope.UNKNOWN, None
    root = parts[0].lower()
    if root in {"groups", "gruppen"}:
        return FileScope.GROUP, parts[1] if len(parts) > 1 else None
    if root in {"files", "eigene", "dateien"}:
        return FileScope.OWN, None
    return FileScope.UNKNOWN, None


class WebDAVClient:
    """PROPFIND/GET/PUT/MKCOL/DELETE against one IServ WebDAV root."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
        verify: bool | str = True,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.root_path = urlparse(self.base_url).path.rstrip("/") + "/"
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.auth = (username, password)
        self.session.verify = verify
        self.session.headers.setdefault("User-Agent", "iserv-scraper")

    # ------------------------------------------------------------------

    def url_for(self, path: str) -> str:
        from urllib.parse import quote

        clean = posixpath.normpath("/" + (path or "").strip("/")).lstrip("/")
        return self.base_url + quote(clean) if clean else self.base_url

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = self.url_for(path)
        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise IServNetworkError(f"WebDAV {method} {url} failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise IServAuthError(
                f"WebDAV rejected the credentials for {url} (HTTP {response.status_code}). "
                "Some schools disable WebDAV — use files.browse(backend='web') instead."
            )
        if response.status_code == 404:
            raise IServNotFound(f"WebDAV path not found: {path}", status=404, url=url)
        if response.status_code >= 400:
            raise IServAPIError(
                f"WebDAV {method} failed", status=response.status_code, url=url,
                body=response.text[:300],
            )
        return response

    # ------------------------------------------------------------------

    def _relative(self, href: str) -> str:
        """Turn an absolute href from the response into a path below the root."""
        path = unquote(urlparse(href).path)
        if path.startswith(self.root_path):
            path = path[len(self.root_path):]
        return path.strip("/")

    def _parse_multistatus(self, xml: bytes, *, skip: str | None) -> list[FileEntry]:
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise IServAPIError(f"Malformed WebDAV response: {exc}") from exc

        entries: list[FileEntry] = []
        for response in root.findall(f"{DAV_NS}response"):
            href_node = response.find(f"{DAV_NS}href")
            if href_node is None or not href_node.text:
                continue
            path = self._relative(href_node.text)
            if skip is not None and path == skip:
                continue        # the collection itself, which Depth:1 includes

            prop = response.find(f"{DAV_NS}propstat/{DAV_NS}prop")
            if prop is None:
                continue

            resource_type = prop.find(f"{DAV_NS}resourcetype")
            is_dir = resource_type is not None and resource_type.find(f"{DAV_NS}collection") is not None

            length = prop.findtext(f"{DAV_NS}getcontentlength")
            scope, group = _scope_of(path)

            entries.append(
                FileEntry(
                    name=prop.findtext(f"{DAV_NS}displayname") or posixpath.basename(path) or path,
                    path=path,
                    is_dir=is_dir,
                    size=int(length) if length and length.isdigit() else None,
                    modified=parse_datetime(prop.findtext(f"{DAV_NS}getlastmodified")),
                    content_type=prop.findtext(f"{DAV_NS}getcontenttype"),
                    etag=(prop.findtext(f"{DAV_NS}getetag") or "").strip('"') or None,
                    scope=scope,
                    group=group,
                    download_url=None if is_dir else self.url_for(path),
                )
            )

        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def list(self, path: str = "", *, depth: int = 1) -> list[FileEntry]:
        """List one directory (``depth=1``) or a whole subtree (``depth="infinity"``)."""
        response = self._request(
            "PROPFIND",
            path,
            data=PROPFIND_BODY.encode("utf-8"),
            headers={"Depth": str(depth), "Content-Type": 'application/xml; charset="utf-8"'},
        )
        return self._parse_multistatus(response.content, skip=path.strip("/"))

    def info(self, path: str) -> FileEntry:
        """Metadata for a single file or folder."""
        response = self._request(
            "PROPFIND",
            path,
            data=PROPFIND_BODY.encode("utf-8"),
            headers={"Depth": "0", "Content-Type": 'application/xml; charset="utf-8"'},
        )
        entries = self._parse_multistatus(response.content, skip=None)
        if not entries:
            raise IServNotFound(f"No WebDAV entry for {path}")
        return entries[0]

    def exists(self, path: str) -> bool:
        try:
            self.info(path)
            return True
        except IServNotFound:
            return False

    def walk(self, path: str = "", *, max_depth: int = 6):
        """Yield every entry below ``path`` breadth-first.

        IServ often refuses ``Depth: infinity``, so this walks level by level.
        """
        queue: list[tuple[str, int]] = [(path, 0)]
        while queue:
            current, depth = queue.pop(0)
            try:
                entries = self.list(current)
            except (IServAPIError, IServAuthError) as exc:
                log.debug("Skipping %s: %s", current, exc)
                continue
            for entry in entries:
                yield entry
                if entry.is_dir and depth + 1 < max_depth:
                    queue.append((entry.path, depth + 1))

    def download(self, path: str) -> bytes:
        return self._request("GET", path).content

    def download_to(self, path: str, destination: str) -> str:
        """Stream a file to disk; returns the destination path."""
        response = self._request("GET", path, stream=True)
        with open(destination, "wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                handle.write(chunk)
        return destination

    def upload(self, path: str, data: bytes | str) -> None:
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._request("PUT", path, data=payload)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        with open(local_path, "rb") as handle:
            self._request("PUT", remote_path, data=handle)

    def mkdir(self, path: str) -> None:
        self._request("MKCOL", path)

    def delete(self, path: str) -> None:
        self._request("DELETE", path)

    def move(self, source: str, target: str, *, overwrite: bool = False) -> None:
        self._request(
            "MOVE", source,
            headers={"Destination": self.url_for(target), "Overwrite": "T" if overwrite else "F"},
        )

    def close(self) -> None:
        self.session.close()
