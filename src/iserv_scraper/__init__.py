"""IServ scraper — a reverse-engineered client for IServ school servers.

Mail, files (own *and* group files), tasks, calendar, notifications and the
address book, all as typed objects that serialise straight to JSON.

    from iserv_scraper import Assistant, IServClient

    with IServClient.from_env() as iserv:
        print(Assistant(iserv).new_stuff()["summary"])
"""

from __future__ import annotations

from .assistant import Assistant
from .client import IServClient
from .config import Settings
from .exceptions import (
    IServAPIError,
    IServAuthError,
    IServConfigError,
    IServError,
    IServModuleUnavailable,
    IServNetworkError,
    IServNotFound,
    IServParseError,
    IServSessionExpired,
)
from .models import (
    Account,
    Badge,
    CalendarEvent,
    Exercise,
    ExerciseStatus,
    FileEntry,
    FileScope,
    Group,
    MailAddress,
    MailAttachment,
    MailFolder,
    MailMessage,
    NewsItem,
    Notification,
    Person,
)
from .webdav import WebDAVClient

__version__ = "1.0.0"

__all__ = [
    "Assistant",
    "IServClient",
    "Settings",
    "WebDAVClient",
    "Account",
    "Badge",
    "CalendarEvent",
    "Exercise",
    "ExerciseStatus",
    "FileEntry",
    "FileScope",
    "Group",
    "MailAddress",
    "MailAttachment",
    "MailFolder",
    "MailMessage",
    "NewsItem",
    "Notification",
    "Person",
    "IServAPIError",
    "IServAuthError",
    "IServConfigError",
    "IServError",
    "IServModuleUnavailable",
    "IServNetworkError",
    "IServNotFound",
    "IServParseError",
    "IServSessionExpired",
    "__version__",
]
