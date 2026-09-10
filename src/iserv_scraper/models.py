"""Typed, JSON-serialisable models for everything the IServ scraper returns.

Same contract as the WebUntis scraper: every model has ``to_dict()`` that emits
plain JSON types, so a result can be handed to an AI assistant unchanged.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


def _jsonify(value: Any) -> Any:
    if isinstance(value, Model):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


@dataclass
class Model:
    """Base class providing JSON conversion."""

    def to_dict(self, *, drop_none: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            if f.metadata.get("exclude"):
                continue
            value = getattr(self, f.name)
            if drop_none and value is None:
                continue
            out[f.name] = _jsonify(value)
        return out


def _human_size(size: int | None) -> str:
    if size is None:
        return "?"
    step = 1024.0
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} PB"


# --------------------------------------------------------------------------
# Mail
# --------------------------------------------------------------------------


@dataclass
class MailFolder(Model):
    """An IMAP folder as shown in the IServ mail module."""

    path: str
    name: str | None = None
    unread: int | None = None
    total: int | None = None
    special_use: str | None = None      # INBOX | Sent | Drafts | Trash | Junk

    def __str__(self) -> str:
        counts = ""
        if self.total is not None:
            counts = f" ({self.unread or 0}/{self.total} ungelesen)"
        return f"{self.name or self.path}{counts}"


@dataclass
class MailAddress(Model):
    name: str | None
    address: str | None

    def __str__(self) -> str:
        if self.name and self.address:
            return f"{self.name} <{self.address}>"
        return self.address or self.name or ""


@dataclass
class MailAttachment(Model):
    filename: str | None
    size: int | None = None
    content_type: str | None = None
    part_id: str | None = None
    download_url: str | None = None

    def __str__(self) -> str:
        return f"{self.filename or 'Anhang'} ({_human_size(self.size)})"


@dataclass
class MailMessage(Model):
    """One e-mail. ``body_text``/``body_html`` are only filled by ``fetch``."""

    uid: int | str | None
    folder: str = "INBOX"
    subject: str | None = None
    sender: MailAddress | None = None
    recipients: list[MailAddress] = field(default_factory=list)
    cc: list[MailAddress] = field(default_factory=list)
    date: datetime | None = None
    size: int | None = None
    flags: list[str] = field(default_factory=list)
    seen: bool | None = None
    answered: bool = False
    flagged: bool = False
    has_attachments: bool = False
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[MailAttachment] = field(default_factory=list)
    message_id: str | None = None
    in_reply_to: str | None = None

    @property
    def unread(self) -> bool:
        return self.seen is False

    def preview(self, length: int = 280) -> str:
        """Short plain-text excerpt — what an assistant usually wants first."""
        text = (self.body_text or "").strip()
        if len(text) <= length:
            return text
        return text[: length - 1].rstrip() + "…"

    def to_dict(self, *, drop_none: bool = False) -> dict[str, Any]:
        data = super().to_dict(drop_none=drop_none)
        data["unread"] = self.unread
        data["preview"] = self.preview()
        return data

    def __str__(self) -> str:
        when = f"{self.date:%d.%m.%Y %H:%M}" if self.date else "?"
        mark = "•" if self.unread else " "
        return f"{mark} {when}  {self.sender or '?'}  —  {self.subject or '(kein Betreff)'}"


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


class FileScope(str, Enum):
    OWN = "own"          # Dateien → Eigene
    GROUP = "group"      # Dateien → Gruppen
    UNKNOWN = "unknown"


@dataclass
class FileEntry(Model):
    """A file or folder in the IServ file module / WebDAV tree."""

    name: str
    path: str                       # WebDAV path, e.g. "Groups/Klasse 10a/Mathe/blatt.pdf"
    is_dir: bool = False
    size: int | None = None
    modified: datetime | None = None
    content_type: str | None = None
    scope: FileScope = FileScope.UNKNOWN
    group: str | None = None
    etag: str | None = None
    download_url: str | None = None

    @property
    def extension(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""

    def __str__(self) -> str:
        if self.is_dir:
            return f"[Ordner] {self.path}"
        when = f", {self.modified:%d.%m.%Y}" if self.modified else ""
        return f"{self.path} ({_human_size(self.size)}{when})"


@dataclass
class Group(Model):
    """An IServ group the user belongs to."""

    name: str
    act: str | None = None          # the internal short name used in URLs
    description: str | None = None
    url: str | None = None
    files_path: str | None = None   # WebDAV path of the group's file folder

    def __str__(self) -> str:
        return self.name


# --------------------------------------------------------------------------
# Exercises (Aufgaben)
# --------------------------------------------------------------------------


class ExerciseStatus(str, Enum):
    CURRENT = "current"
    PAST = "past"
    DONE = "done"
    UNKNOWN = "unknown"


@dataclass
class Exercise(Model):
    """A task from the IServ "Aufgaben" module."""

    id: int | str | None
    title: str | None
    start: datetime | None = None
    deadline: datetime | None = None
    description: str | None = None
    status: ExerciseStatus = ExerciseStatus.UNKNOWN
    owner: str | None = None
    tags: list[str] = field(default_factory=list)
    attachments: list[FileEntry] = field(default_factory=list)
    submission_required: bool | None = None
    submitted: bool | None = None
    feedback: str | None = None
    grade: str | None = None
    url: str | None = None

    def days_left(self, now: datetime | None = None) -> int | None:
        if self.deadline is None:
            return None
        return (self.deadline - (now or datetime.now())).days

    def __str__(self) -> str:
        due = f" (bis {self.deadline:%d.%m.%Y %H:%M})" if self.deadline else ""
        return f"{self.title or 'Aufgabe'}{due}"


# --------------------------------------------------------------------------
# Calendar / notifications / people
# --------------------------------------------------------------------------


@dataclass
class CalendarEvent(Model):
    id: str | int | None
    title: str | None
    start: datetime | None
    end: datetime | None
    all_day: bool = False
    location: str | None = None
    description: str | None = None
    calendar: str | None = None
    url: str | None = None

    def __str__(self) -> str:
        when = f"{self.start:%d.%m.%Y %H:%M}" if self.start else "?"
        return f"{when} {self.title or ''}".strip()


@dataclass
class Notification(Model):
    id: int | str | None
    module: str | None = None
    title: str | None = None
    text: str | None = None
    date: datetime | None = None
    link: str | None = None
    read: bool | None = None

    def __str__(self) -> str:
        return f"[{self.module or '?'}] {self.title or self.text or ''}".strip()


@dataclass
class Badge(Model):
    """Unread counter of one module, as shown in the IServ navigation."""

    module: str
    count: int

    def __str__(self) -> str:
        return f"{self.module}: {self.count}"


@dataclass
class Person(Model):
    """An entry from the public address book."""

    name: str
    username: str | None = None
    email: str | None = None
    url: str | None = None
    groups: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.name}" + (f" <{self.email}>" if self.email else "")


@dataclass
class NewsItem(Model):
    """An article from the IServ news module."""

    id: int | str | None
    title: str | None
    text: str | None = None
    author: str | None = None
    published: datetime | None = None
    url: str | None = None

    def __str__(self) -> str:
        when = f"{self.published:%d.%m.%Y}" if self.published else ""
        return f"{when} {self.title or ''}".strip()


@dataclass
class Account(Model):
    """Who we are logged in as."""

    username: str
    display_name: str | None = None
    email: str | None = None
    domain: str | None = None
    groups: list[Group] = field(default_factory=list)
    quota_used: int | None = None
    quota_total: int | None = None

    @property
    def quota_percent(self) -> float | None:
        if not self.quota_total:
            return None
        return round(100 * (self.quota_used or 0) / self.quota_total, 1)

    def to_dict(self, *, drop_none: bool = False) -> dict[str, Any]:
        data = super().to_dict(drop_none=drop_none)
        data["quota_percent"] = self.quota_percent
        return data

    def __str__(self) -> str:
        return f"{self.display_name or self.username} ({self.email or self.username})"
