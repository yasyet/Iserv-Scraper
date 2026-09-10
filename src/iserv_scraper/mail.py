"""The mail module — two interchangeable backends.

``imap`` (preferred)
    IServ *is* a mail server, so it speaks IMAPS on port 993 with the same
    credentials. This gives complete messages, real flags, server-side search
    and attachments without scraping anything. It is the reliable path.

``web``
    When a school blocks IMAP from outside, the browser interface is still
    reachable. These endpoints were reverse-engineered from the mail module:

    ==========================================  ==========================
    ``GET /iserv/mail/api/folder/list``          folder tree as JSON
    ``GET /iserv/mail/api/message/list``         message list (DataTables:
                                                 ``path``, ``start``,
                                                 ``length``, ``order[column]``,
                                                 ``order[dir]``)
    ``GET /iserv/mail/show/source``              the raw RFC-822 message
                                                 (``path``, ``msg``)
    ``GET /iserv/mail/show``                     rendered message (HTML)
    ==========================================  ==========================

``auto`` (default) tries IMAP once and falls back to the web backend.
"""

from __future__ import annotations

import base64
import binascii
import imaplib
import logging
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Iterable

from .exceptions import IServAPIError, IServError, IServModuleUnavailable, IServNotFound
from .models import MailFolder, MailMessage
from .parsing import extract_php_data, parse_mail_folders, parse_mail_list, parse_rfc822

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# IMAP modified UTF-7 (RFC 3501) — folder names such as "Entw&APw-rfe"
# --------------------------------------------------------------------------


def decode_imap_utf7(value: str) -> str:
    def _decode(match: re.Match[str]) -> str:
        chunk = match.group(1)
        if not chunk:
            return "&"
        padded = chunk.replace(",", "/")
        padded += "=" * (-len(padded) % 4)
        try:
            return base64.b64decode(padded).decode("utf-16-be")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return match.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", _decode, value)


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------


class IMAPBackend:
    """Talks IMAPS to the IServ mail server."""

    def __init__(self, host: str, username: str, password: str, *, port: int = 993,
                 timeout: float = 20.0) -> None:
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.timeout = timeout
        self._connection: imaplib.IMAP4_SSL | None = None
        self._selected: str | None = None

    # ----------------------------------------------------------

    def connect(self) -> imaplib.IMAP4_SSL:
        if self._connection is not None:
            try:
                self._connection.noop()
                return self._connection
            except (imaplib.IMAP4.error, OSError):
                self._connection = None
                self._selected = None

        try:
            connection = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
            connection.login(self.username, self._password)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise IServModuleUnavailable(f"IMAP login to {self.host}:{self.port} failed: {exc}") from exc

        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            self._connection = None
            self._selected = None

    def _select(self, folder: str, readonly: bool = True) -> imaplib.IMAP4_SSL:
        connection = self.connect()
        if self._selected != (folder, readonly):
            status, _ = connection.select(f'"{folder}"', readonly=readonly)
            if status != "OK":
                raise IServNotFound(f"IMAP folder not found: {folder}")
            self._selected = (folder, readonly)  # type: ignore[assignment]
        return connection

    # ----------------------------------------------------------

    def folders(self) -> list[MailFolder]:
        connection = self.connect()
        status, lines = connection.list()
        if status != "OK":
            raise IServAPIError("IMAP LIST failed")

        folders: list[MailFolder] = []
        for line in lines or []:
            text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
            match = re.match(r'\((?P<flags>[^)]*)\)\s+"?(?P<delim>[^"\s]*)"?\s+(?P<name>.+)$', text)
            if not match:
                continue
            raw_name = match.group("name").strip().strip('"')
            flags = match.group("flags")
            special = next(
                (f.lstrip("\\") for f in flags.split()
                 if f.lstrip("\\") in {"Sent", "Drafts", "Trash", "Junk", "Archive"}),
                None,
            )
            folders.append(
                MailFolder(
                    path=raw_name,
                    name=decode_imap_utf7(raw_name).split(match.group("delim") or ".")[-1],
                    special_use=special or ("INBOX" if raw_name.upper() == "INBOX" else None),
                )
            )
        return folders

    def folder_status(self, folder: str) -> tuple[int | None, int | None]:
        connection = self.connect()
        status, data = connection.status(f'"{folder}"', "(MESSAGES UNSEEN)")
        if status != "OK" or not data:
            return None, None
        text = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
        total = re.search(r"MESSAGES\s+(\d+)", text)
        unseen = re.search(r"UNSEEN\s+(\d+)", text)
        return (int(total.group(1)) if total else None,
                int(unseen.group(1)) if unseen else None)

    def search(self, folder: str, criteria: str = "ALL") -> list[int]:
        connection = self._select(folder)
        status, data = connection.uid("SEARCH", None, criteria)  # type: ignore[arg-type]
        if status != "OK":
            raise IServAPIError(f"IMAP SEARCH failed for {folder}: {criteria}")
        raw = (data[0] or b"").decode() if data else ""
        return [int(uid) for uid in raw.split()]

    def headers(self, folder: str, uids: Iterable[int]) -> list[MailMessage]:
        uids = list(uids)
        if not uids:
            return []
        connection = self._select(folder)
        parts = "(UID FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER])"
        status, data = connection.uid("FETCH", ",".join(str(u) for u in uids), parts)
        if status != "OK":
            raise IServAPIError(f"IMAP FETCH failed for {folder}")

        messages: list[MailMessage] = []
        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
            message = parse_rfc822(item[1], folder=folder)

            uid_match = re.search(r"UID\s+(\d+)", meta)
            message.uid = int(uid_match.group(1)) if uid_match else None
            size_match = re.search(r"RFC822\.SIZE\s+(\d+)", meta)
            message.size = int(size_match.group(1)) if size_match else None

            flags_match = re.search(r"FLAGS\s+\(([^)]*)\)", meta)
            flags = flags_match.group(1).split() if flags_match else []
            message.flags = flags
            lowered = {f.lower().lstrip("\\") for f in flags}
            message.seen = "seen" in lowered
            message.answered = "answered" in lowered
            message.flagged = "flagged" in lowered
            message.has_attachments = bool(message.attachments) or (
                "attachment" in meta.lower()
            )
            # headers only: drop the empty body so `preview` is not misleading
            message.body_text = None
            message.body_html = None
            messages.append(message)

        messages.sort(key=lambda m: m.date or datetime.min, reverse=True)
        return messages

    def fetch(self, folder: str, uid: int) -> MailMessage:
        connection = self._select(folder)
        status, data = connection.uid("FETCH", str(uid), "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise IServNotFound(f"Message {uid} not found in {folder}")
        return parse_rfc822(data[0][1], uid=uid, folder=folder)

    def set_flag(self, folder: str, uid: int, flag: str, value: bool = True) -> None:
        connection = self._select(folder, readonly=False)
        connection.uid("STORE", str(uid), "+FLAGS" if value else "-FLAGS", f"({flag})")


class WebMailBackend:
    """Uses the endpoints behind the IServ mail web interface."""

    FOLDER_LIST = "iserv/mail/api/folder/list"
    MESSAGE_LIST = "iserv/mail/api/message/list"
    MESSAGE_SOURCE = "iserv/mail/show/source"

    def __init__(self, transport) -> None:
        self.transport = transport

    def folders(self) -> list[MailFolder]:
        payload = self.transport.get_json(self.FOLDER_LIST, optional=True)
        return parse_mail_folders(payload)

    def list(self, folder: str, *, limit: int = 50, offset: int = 0,
             order: str = "date", direction: str = "desc",
             search: str | None = None) -> list[MailMessage]:
        params = {
            "path": folder,
            "length": limit,
            "start": offset,
            "order[column]": order,
            "order[dir]": direction,
        }
        if search:
            params["search[value]"] = search

        response = self.transport.request("GET", self.MESSAGE_LIST, params=params)
        if response.status_code >= 400:
            raise IServModuleUnavailable(
                "The mail module did not answer", status=response.status_code, url=response.url
            )

        # Depending on the IServ version this is either JSON or an HTML page
        # with the payload inside <script id="php-data">.
        try:
            payload: Any = response.json()
        except ValueError:
            payload = extract_php_data(response.text)
            if payload is None:
                raise IServModuleUnavailable(
                    "Could not read the message list (neither JSON nor php-data)",
                    status=response.status_code, url=response.url,
                ) from None
        return parse_mail_list(payload, folder=folder)

    def fetch(self, folder: str, uid: int) -> MailMessage:
        response = self.transport.request(
            "GET", self.MESSAGE_SOURCE, params={"path": folder, "msg": uid}
        )
        if response.status_code >= 400 or not response.content:
            raise IServNotFound(f"Message {uid} not found in {folder}", status=response.status_code)
        return parse_rfc822(response.content, uid=uid, folder=folder)


# --------------------------------------------------------------------------
# public module
# --------------------------------------------------------------------------


class MailModule:
    """Mail access with a backend chosen at runtime."""

    def __init__(self, client, *, backend: str = "auto", imap_host: str | None = None,
                 imap_port: int = 993) -> None:
        self.client = client
        self.preferred = backend
        self.web = WebMailBackend(client.transport)
        self.imap = IMAPBackend(
            imap_host or client.domain, client.username, client._password,
            port=imap_port, timeout=client.transport.timeout,
        )
        self._active: str | None = None if backend == "auto" else backend

    # ----------------------------------------------------------

    @property
    def backend(self) -> str:
        """Which backend is in use — resolves ``auto`` on first access."""
        if self._active is None:
            try:
                self.imap.connect()
                self._active = "imap"
            except IServError as exc:
                log.info("IMAP unavailable (%s) — falling back to the web interface", exc)
                self._active = "web"
        return self._active

    def use(self, backend: str) -> None:
        """Force a backend (``imap`` or ``web``)."""
        if backend not in {"imap", "web", "auto"}:
            raise ValueError("backend must be 'imap', 'web' or 'auto'")
        self._active = None if backend == "auto" else backend

    # ----------------------------------------------------------

    def folders(self, *, with_counts: bool = False) -> list[MailFolder]:
        """All mail folders. ``with_counts`` adds message/unread counts (IMAP only)."""
        if self.backend == "imap":
            folders = self.imap.folders()
            if with_counts:
                for folder in folders:
                    folder.total, folder.unread = self.imap.folder_status(folder.path)
            return folders
        return self.web.folders()

    def list(
        self,
        folder: str = "INBOX",
        *,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
        search: str | None = None,
    ) -> list[MailMessage]:
        """Message headers of one folder, newest first."""
        cache_key = f"mail:{self.backend}:{folder}:{limit}:{offset}:{unread_only}:{search}"
        cached = self.client.cache.get(cache_key)
        if cached is not None:
            return cached

        if self.backend == "imap":
            criteria = "UNSEEN" if unread_only else "ALL"
            if search:
                escaped = search.replace('"', '\\"')
                criteria = f'({criteria} TEXT "{escaped}")' if unread_only else f'TEXT "{escaped}"'
            uids = self.imap.search(folder, criteria)
            window = uids[::-1][offset: offset + limit]   # newest first
            messages = self.imap.headers(folder, window)
        else:
            messages = self.web.list(folder, limit=limit, offset=offset, search=search)
            if unread_only:
                messages = [m for m in messages if m.unread]

        self.client.cache.set(cache_key, messages)
        return messages

    def unread(self, folder: str = "INBOX", limit: int = 50) -> list[MailMessage]:
        return self.list(folder, limit=limit, unread_only=True)

    def fetch(self, uid: int, folder: str = "INBOX") -> MailMessage:
        """One complete message including bodies and attachment metadata."""
        if self.backend == "imap":
            return self.imap.fetch(folder, uid)
        return self.web.fetch(folder, uid)

    def search(self, query: str, folder: str = "INBOX", *, limit: int = 50) -> list[MailMessage]:
        """Full-text search inside a folder."""
        return self.list(folder, limit=limit, search=query)

    def attachments(self, uid: int, folder: str = "INBOX") -> list[Any]:
        return self.fetch(uid, folder).attachments

    def download_attachment(self, uid: int, filename: str, folder: str = "INBOX") -> bytes:
        """Return the bytes of one attachment of a message."""
        import email
        from email import policy

        raw = self._raw(uid, folder)
        message = email.message_from_bytes(raw, policy=policy.default)
        for part in message.walk():
            if part.get_filename() == filename:
                return part.get_payload(decode=True) or b""
        raise IServNotFound(f"Attachment {filename!r} not found in message {uid}")

    def _raw(self, uid: int, folder: str) -> bytes:
        if self.backend == "imap":
            connection = self.imap._select(folder)
            status, data = connection.uid("FETCH", str(uid), "(RFC822)")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                raise IServNotFound(f"Message {uid} not found in {folder}")
            return data[0][1]
        response = self.client.transport.request(
            "GET", WebMailBackend.MESSAGE_SOURCE, params={"path": folder, "msg": uid}
        )
        return response.content

    def mark_read(self, uid: int, folder: str = "INBOX", read: bool = True) -> None:
        """Set or clear the ``\\Seen`` flag (IMAP backend only)."""
        if self.backend != "imap":
            raise IServModuleUnavailable("Marking messages read requires the IMAP backend")
        self.imap.set_flag(folder, uid, "\\Seen", read)
        self.client.cache.invalidate("mail:")

    def send(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        *,
        html: str | None = None,
        cc: str | list[str] | None = None,
        attachments: list[str] | None = None,
        smtp_host: str | None = None,
        smtp_port: int = 465,
    ) -> None:
        """Send a mail over the school's SMTPS server (port 465)."""
        import mimetypes
        import os

        def _addresses(value: str | list[str] | None) -> list[str]:
            if not value:
                return []
            return [value] if isinstance(value, str) else list(value)

        message = EmailMessage()
        message["From"] = self.client.account_email
        message["To"] = ", ".join(_addresses(to))
        if cc:
            message["Cc"] = ", ".join(_addresses(cc))
        message["Subject"] = subject
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")

        for path in attachments or []:
            guessed, _ = mimetypes.guess_type(path)
            maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
            with open(path, "rb") as handle:
                message.add_attachment(
                    handle.read(), maintype=maintype, subtype=subtype,
                    filename=os.path.basename(path),
                )

        host = smtp_host or self.client.domain
        try:
            with smtplib.SMTP_SSL(host, smtp_port, timeout=self.client.transport.timeout) as server:
                server.login(self.client.username, self.client._password)
                server.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise IServAPIError(f"Sending via {host}:{smtp_port} failed: {exc}") from exc

    def close(self) -> None:
        self.imap.close()
