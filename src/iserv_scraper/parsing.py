"""Turning IServ's HTML and JSON into models.

IServ mixes three response styles, all handled here:

1. **DataTables JSON** — the mail list and a few other tables answer with
   ``{"data": [...], "recordsTotal": N}``.
2. **``<script id="php-data">``** — several pages embed their JSON payload in a
   script tag instead of offering an endpoint.
3. **Plain HTML** — the exercise and address book modules only exist as pages,
   so they are scraped with BeautifulSoup.

Everything here is defensive: an IServ update changes markup, and a partially
parsed list beats an exception.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable, Mapping

from .exceptions import IServParseError
from .models import (
    Badge,
    CalendarEvent,
    Exercise,
    ExerciseStatus,
    MailAddress,
    MailAttachment,
    MailFolder,
    MailMessage,
    NewsItem,
    Notification,
    Person,
)

log = logging.getLogger(__name__)


def soup_of(html: str):
    """Build a BeautifulSoup document, preferring lxml when it is installed."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise IServParseError(
            "beautifulsoup4 is required for the HTML-based modules: pip install beautifulsoup4"
        ) from exc

    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:  # noqa: BLE001 - lxml may be missing
            continue
    raise IServParseError("No usable HTML parser available")


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


_GERMAN_FORMATS = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%d.%m.%y %H:%M",
    "%d.%m.%y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def parse_datetime(value: Any) -> datetime | None:
    """Parse the many timestamp shapes IServ emits.

    Accepts ISO-8601 (with or without ``Z``), German ``TT.MM.JJJJ HH:MM``,
    RFC 2822 mail dates and unix timestamps.
    """
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    for fmt in _GERMAN_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    try:  # RFC 2822, as used in raw mail headers
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        pass

    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text))
        except (OverflowError, OSError, ValueError):
            return None
    return None


_SIZE_UNITS = {"b": 1, "byte": 1, "bytes": 1, "kb": 1024, "kib": 1024,
               "mb": 1024**2, "mib": 1024**2, "gb": 1024**3, "gib": 1024**3,
               "tb": 1024**4}


def parse_size(value: Any) -> int | None:
    """``"1,2 MB"`` / ``"345 KB"`` / ``12345`` -> bytes."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace("\xa0", " ")
    if text.isdigit():
        return int(text)
    match = re.match(r"([\d.,]+)\s*([a-z]+)", text)
    if not match:
        return None
    number = match.group(1).replace(".", "").replace(",", ".") if "," in match.group(1) else match.group(1)
    try:
        return int(float(number) * _SIZE_UNITS.get(match.group(2), 1))
    except ValueError:
        return None


def first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, "", [], {}):
            return mapping[key]
    return default


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def text_of(node: Any, default: str | None = None) -> str | None:
    """``node.get_text()`` that survives ``None`` and collapses whitespace."""
    if node is None:
        return default
    text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)
    text = re.sub(r"\s+", " ", text).strip()
    return text or default


def extract_php_data(html: str, element_id: str = "php-data") -> Any:
    """Read the JSON payload IServ embeds in ``<script id="php-data">``.

    Used by the mail module (and a few others) instead of a real endpoint.
    """
    match = re.search(
        rf'<script[^>]*id=["\']{re.escape(element_id)}["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return None
    raw = match.group(1).strip().strip("()").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise IServParseError(
            f"Could not decode the {element_id} payload: {exc}", snippet=raw[:300]
        ) from exc


# --------------------------------------------------------------------------
# mail
# --------------------------------------------------------------------------


def _mail_address(value: Any) -> MailAddress | None:
    """Normalise the several shapes IServ uses for an address."""
    if value in (None, ""):
        return None
    if isinstance(value, Mapping):
        return MailAddress(
            name=first(value, "personal", "name", "displayName"),
            address=first(value, "mail", "address", "email"),
        )
    if isinstance(value, (list, tuple)):
        return _mail_address(value[0]) if value else None

    text = str(value).strip()
    match = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$', text)
    if match:
        return MailAddress(name=match.group(1).strip() or None, address=match.group(2).strip())
    return MailAddress(name=None, address=text) if "@" in text else MailAddress(name=text, address=None)


def _mail_addresses(value: Any) -> list[MailAddress]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r",(?![^<]*>)", value) if p.strip()]
        return [addr for addr in (_mail_address(p) for p in parts) if addr]
    return [addr for addr in (_mail_address(v) for v in as_list(value)) if addr]


def parse_mail_folders(payload: Any) -> list[MailFolder]:
    """Parse ``/iserv/mail/api/folder/list``."""
    entries: Iterable[Any]
    if isinstance(payload, Mapping):
        entries = first(payload, "folders", "data", "items", default=[]) or []
    else:
        entries = payload or []

    folders: list[MailFolder] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        path = first(raw, "path", "id", "key", "name")
        if not path:
            continue
        folders.append(
            MailFolder(
                path=str(path),
                name=first(raw, "name", "displayName", "title", "text") or str(path),
                unread=_as_int(first(raw, "unseen", "unread", "unreadCount")),
                total=_as_int(first(raw, "total", "messages", "count", "exists")),
                special_use=first(raw, "specialUse", "special_use", "type"),
            )
        )
        # Some IServ versions nest sub-folders instead of flattening them.
        for child in as_list(first(raw, "children", "folders", default=[])):
            if isinstance(child, Mapping):
                folders.extend(parse_mail_folders([child]))
    return folders


def _as_int(value: Any) -> int | None:
    # NB: `value in (None, "", False)` would also swallow a legitimate 0,
    # because 0 == False in Python — hence the explicit checks.
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_mail_list(payload: Any, folder: str = "INBOX") -> list[MailMessage]:
    """Parse ``/iserv/mail/api/message/list`` (a DataTables response)."""
    rows: Iterable[Any]
    if isinstance(payload, Mapping):
        rows = first(payload, "data", "messages", "items", "aaData", default=[]) or []
    else:
        rows = payload or []

    messages: list[MailMessage] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        flags = [str(f) for f in as_list(first(raw, "flags", "flag", default=[]))]
        lowered = {f.lower().lstrip("\\") for f in flags}

        seen = first(raw, "seen", "isSeen")
        if seen is None:
            unseen = first(raw, "unseen", "unread", "isUnread")
            seen = (not unseen) if unseen is not None else ("seen" in lowered or None)

        messages.append(
            MailMessage(
                uid=_as_int(first(raw, "uid", "id", "msg", "messageId")),
                folder=str(first(raw, "path", "folder", default=folder)),
                subject=first(raw, "subject", "title"),
                sender=_mail_address(first(raw, "from", "sender", "fromAddress")),
                recipients=_mail_addresses(first(raw, "to", "recipients")),
                cc=_mail_addresses(raw.get("cc")),
                date=parse_datetime(first(raw, "date", "sentDate", "receivedDate", "timestamp")),
                size=parse_size(first(raw, "size", "bytes")),
                flags=flags,
                seen=bool(seen) if seen is not None else None,
                answered=bool(first(raw, "answered", default="answered" in lowered)),
                flagged=bool(first(raw, "flagged", default="flagged" in lowered)),
                has_attachments=bool(
                    first(raw, "hasAttachments", "attachments", "attachment", default=False)
                ),
            )
        )

    messages.sort(key=lambda m: m.date or datetime.min, reverse=True)
    return messages


def parse_rfc822(raw: bytes | str, *, uid: Any = None, folder: str = "INBOX") -> MailMessage:
    """Turn a raw RFC-822 message into a :class:`MailMessage` with bodies.

    Used by both backends: the web backend fetches
    ``/iserv/mail/show/source``, the IMAP backend fetches ``RFC822``.
    """
    import email
    from email import policy

    if isinstance(raw, str):
        message = email.message_from_string(raw, policy=policy.default)
    else:
        message = email.message_from_bytes(raw, policy=policy.default)

    body_text: str | None = None
    body_html: str | None = None
    attachments: list[MailAttachment] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        content_type = part.get_content_type()
        if disposition == "attachment" or (disposition == "inline" and part.get_filename()):
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                MailAttachment(
                    filename=part.get_filename(),
                    size=len(payload),
                    content_type=content_type,
                    part_id=part.get("Content-ID"),
                )
            )
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError):  # unknown charset
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if content_type == "text/plain" and body_text is None:
            body_text = content
        elif content_type == "text/html" and body_html is None:
            body_html = content

    if body_text is None and body_html:
        body_text = text_of(soup_of(body_html))

    return MailMessage(
        uid=uid,
        folder=folder,
        subject=str(message.get("Subject") or "") or None,
        sender=_mail_address(str(message.get("From") or "")),
        recipients=_mail_addresses(str(message.get("To") or "")),
        cc=_mail_addresses(str(message.get("Cc") or "")),
        date=parse_datetime(message.get("Date")),
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        has_attachments=bool(attachments),
        message_id=str(message.get("Message-ID") or "") or None,
        in_reply_to=str(message.get("In-Reply-To") or "") or None,
    )


# --------------------------------------------------------------------------
# notifications / badges / calendar
# --------------------------------------------------------------------------


def parse_notifications(payload: Any) -> list[Notification]:
    """Parse ``/iserv/user/api/notifications``."""
    entries: Iterable[Any]
    if isinstance(payload, Mapping):
        entries = first(payload, "notifications", "data", "items", default=[]) or []
    else:
        entries = payload or []

    result: list[Notification] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        result.append(
            Notification(
                id=first(raw, "id", "notificationId"),
                module=first(raw, "module", "type", "source", "app"),
                title=first(raw, "title", "subject", "headline"),
                text=first(raw, "text", "message", "body", "description"),
                date=parse_datetime(first(raw, "date", "createdAt", "time", "timestamp")),
                link=first(raw, "link", "url", "href"),
                read=first(raw, "read", "isRead", "seen"),
            )
        )
    result.sort(key=lambda n: n.date or datetime.min, reverse=True)
    return result


def parse_badges(payload: Any) -> list[Badge]:
    """Parse ``/iserv/app/navigation/badges`` (module -> unread count)."""
    if isinstance(payload, Mapping):
        source = payload.get("badges") if isinstance(payload.get("badges"), Mapping) else payload
        badges: list[Badge] = []
        for module, value in (source or {}).items():
            count = value
            if isinstance(value, Mapping):
                count = first(value, "count", "value", "badge", default=0)
            number = _as_int(count)
            if number is not None:
                badges.append(Badge(module=str(module), count=number))
        return sorted(badges, key=lambda b: b.module)
    return []


def parse_calendar_events(payload: Any, calendar: str | None = None) -> list[CalendarEvent]:
    """Parse a FullCalendar feed (``/iserv/calendar/feed/...``)."""
    entries: Iterable[Any]
    if isinstance(payload, Mapping):
        entries = first(payload, "events", "data", "items", default=[]) or []
    else:
        entries = payload or []

    events: list[CalendarEvent] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        events.append(
            CalendarEvent(
                id=first(raw, "id", "uid", "eventId"),
                title=first(raw, "title", "summary", "name"),
                start=parse_datetime(first(raw, "start", "startDate", "dtstart")),
                end=parse_datetime(first(raw, "end", "endDate", "dtend")),
                all_day=bool(first(raw, "allDay", "all_day", default=False)),
                location=first(raw, "location", "place"),
                description=first(raw, "description", "text", "body"),
                calendar=first(raw, "calendar", "source", "className") or calendar,
                url=first(raw, "url", "link"),
            )
        )
    events.sort(key=lambda e: e.start or datetime.max)
    return events


# --------------------------------------------------------------------------
# exercises (HTML)
# --------------------------------------------------------------------------

_EXERCISE_LINK = re.compile(r"/iserv/exercise/show/(\d+)")


def parse_exercise_list(html: str, status: ExerciseStatus = ExerciseStatus.UNKNOWN,
                        base_url: str = "") -> list[Exercise]:
    """Scrape the exercise overview page (``/iserv/exercise``).

    The overview is a table of links to ``/iserv/exercise/show/<id>``; the
    remaining columns (deadline, tags, submission state) vary by IServ version,
    so only what is recognisable is filled in — :func:`parse_exercise_detail`
    provides the rest.
    """
    document = soup_of(html)
    seen: set[str] = set()
    exercises: list[Exercise] = []

    for link in document.find_all("a", href=True):
        match = _EXERCISE_LINK.search(link["href"])
        if not match or match.group(1) in seen:
            continue
        exercise_id = match.group(1)
        seen.add(exercise_id)

        row = link.find_parent("tr")
        cells = [text_of(td) for td in row.find_all("td")] if row else []
        deadline = next(
            (parsed for cell in cells if (parsed := parse_datetime(cell)) is not None), None
        )

        exercises.append(
            Exercise(
                id=int(exercise_id),
                title=text_of(link),
                deadline=deadline,
                status=status,
                url=f"{base_url.rstrip('/')}/iserv/exercise/show/{exercise_id}" if base_url else link["href"],
                submitted=any("abgegeben" in (c or "").lower() for c in cells) or None,
            )
        )
    return exercises


def parse_exercise_detail(html: str, exercise_id: Any = None, base_url: str = "") -> Exercise:
    """Scrape ``/iserv/exercise/show/<id>``."""
    from .models import FileEntry

    document = soup_of(html)

    title = text_of(document.find("h1")) or text_of(document.find("h2"))
    if title:
        for prefix in ("Details for ", "Details zu ", "Aufgabe: "):
            if title.startswith(prefix):
                title = title[len(prefix):]

    description_node = document.find(class_="text-break-word") or document.find(
        class_="exercise-description"
    )
    description = text_of(description_node)

    # The detail page renders start/deadline in a two-column table.
    start = deadline = None
    for row in document.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = (text_of(cells[0]) or "").lower()
        value = parse_datetime(text_of(cells[1]))
        if value is None:
            continue
        if "start" in label or "beginn" in label:
            start = value
        elif "ende" in label or "abgabe" in label or "deadline" in label or "due" in label:
            deadline = value

    attachments = []
    for link in document.find_all("a", href=True):
        href = link["href"]
        if "/iserv/fs/file/" in href or "/iserv/exercise/attachment" in href:
            name = text_of(link) or href.rsplit("/", 1)[-1]
            attachments.append(
                FileEntry(
                    name=name,
                    path=href,
                    is_dir=False,
                    download_url=href if href.startswith("http") else f"{base_url.rstrip('/')}{href}",
                )
            )

    body_text = document.get_text(" ", strip=True).lower() if document else ""

    return Exercise(
        id=_as_int(exercise_id) or exercise_id,
        title=title,
        start=start,
        deadline=deadline,
        description=description,
        attachments=attachments,
        submitted="abgegeben" in body_text or "submitted" in body_text or None,
        url=f"{base_url.rstrip('/')}/iserv/exercise/show/{exercise_id}" if base_url and exercise_id else None,
    )


# --------------------------------------------------------------------------
# address book / news (HTML)
# --------------------------------------------------------------------------


def parse_address_book(html: str, base_url: str = "") -> list[Person]:
    """Scrape ``/iserv/addressbook/public``."""
    document = soup_of(html)
    people: list[Person] = []

    for link in document.find_all("a", href=True):
        if "/iserv/addressbook/public/show/" not in link["href"]:
            continue
        name = text_of(link)
        if not name:
            continue
        username = link["href"].rsplit("/", 1)[-1]
        row = link.find_parent("tr")
        email = None
        if row:
            mail_link = row.find("a", href=re.compile(r"^mailto:"))
            if mail_link:
                email = mail_link["href"][len("mailto:"):]
        people.append(
            Person(
                name=name,
                username=username,
                email=email,
                url=link["href"] if link["href"].startswith("http")
                else f"{base_url.rstrip('/')}{link['href']}",
            )
        )
    return people


def parse_person_detail(html: str, username: str | None = None) -> Person:
    """Scrape ``/iserv/addressbook/public/show/<user>`` into a flat dict."""
    document = soup_of(html)
    details: dict[str, Any] = {}

    for row in document.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            key = (text_of(cells[0]) or "").rstrip(":")
            value = text_of(cells[1])
            if key and value:
                details[key] = value

    name = text_of(document.find("h1")) or details.get("Name") or username or "?"
    email = next(
        (v for k, v in details.items() if "mail" in k.lower() and "@" in str(v)), None
    )
    groups = [
        g.strip()
        for key, value in details.items()
        if "gruppe" in key.lower() or "group" in key.lower()
        for g in str(value).split(",")
        if g.strip()
    ]
    return Person(name=name, username=username, email=email, groups=groups, details=details)


def parse_news(html: str, base_url: str = "") -> list[NewsItem]:
    """Scrape ``/iserv/news`` — best effort, the markup differs per theme."""
    document = soup_of(html)
    items: list[NewsItem] = []

    for article in document.find_all(["article", "div"], class_=re.compile(r"news|panel|card")):
        heading = article.find(["h1", "h2", "h3", "h4"])
        title = text_of(heading)
        if not title:
            continue
        link = heading.find("a", href=True) or article.find("a", href=True)
        href = link["href"] if link else None
        identifier = None
        if href:
            match = re.search(r"/news/(?:show/)?(\d+)", href)
            identifier = _as_int(match.group(1)) if match else None
        time_node = article.find("time")
        published = parse_datetime(
            time_node.get("datetime") if time_node and time_node.has_attr("datetime")
            else text_of(time_node)
        )
        items.append(
            NewsItem(
                id=identifier,
                title=title,
                text=text_of(article.find("p")),
                published=published,
                url=(href if href and href.startswith("http")
                     else f"{base_url.rstrip('/')}{href}" if href else None),
            )
        )
    return items
