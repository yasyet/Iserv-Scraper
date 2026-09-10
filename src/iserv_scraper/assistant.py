"""Aggregated views for an AI assistant.

:class:`~iserv_scraper.client.IServClient` mirrors IServ module by module.
This layer answers the questions that actually get asked — "was gibt es Neues?",
"welche Aufgaben sind offen?", "wo liegt die Datei?" — and returns one flat,
JSON-serialisable dict per question, including a German ``summary`` sentence.

Every section is guarded individually: a school without the exercise module
still gets its mail, files and calendar, and the missing part is reported in
``unavailable`` instead of raising.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, TypeVar

from .client import IServClient
from .exceptions import IServError, IServModuleUnavailable
from .models import Model

log = logging.getLogger(__name__)

T = TypeVar("T")


def _dump(value: Any) -> Any:
    if isinstance(value, Model):
        return value.to_dict()
    if isinstance(value, list):
        return [_dump(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    return value


class Assistant:
    """Ready-to-answer views over an :class:`IServClient`."""

    def __init__(self, client: IServClient) -> None:
        self.client = client

    # ------------------------------------------------------------------

    def _safe(self, name: str, call: Callable[[], T], unavailable: list[str],
              errors: dict[str, str], default: T) -> T:
        try:
            return call()
        except IServModuleUnavailable:
            unavailable.append(name)
        except IServError as exc:
            log.warning("Section %s failed: %s", name, exc)
            errors[name] = str(exc)
        except Exception as exc:  # noqa: BLE001 - an overview must never crash
            log.warning("Section %s crashed: %s", name, exc)
            errors[name] = f"{type(exc).__name__}: {exc}"
        return default

    # ------------------------------------------------------------------

    def inbox(self, *, limit: int = 15, unread_only: bool = True,
              with_preview: bool = False) -> dict[str, Any]:
        """The mailbox at a glance.

        ``with_preview=True`` fetches each message body so the assistant can
        summarise without a second round trip — one request per mail, so keep
        ``limit`` small.
        """
        unavailable: list[str] = []
        errors: dict[str, str] = {}

        messages = self._safe(
            "mail",
            lambda: self.client.mail.list(limit=limit, unread_only=unread_only),
            unavailable, errors, [],
        )

        if with_preview and messages:
            filled = []
            for message in messages:
                try:
                    filled.append(self.client.mail.fetch(message.uid, message.folder))
                except IServError as exc:
                    log.debug("Could not fetch mail %s: %s", message.uid, exc)
                    filled.append(message)
            messages = filled

        unread = [m for m in messages if m.unread]
        return {
            "backend": self.client.mail.backend if not unavailable else None,
            "summary": (
                f"{len(unread)} ungelesene von {len(messages)} angezeigten Mails."
                if messages else "Keine Mails gefunden."
            ),
            "count": len(messages),
            "unread_count": len(unread),
            "messages": _dump(messages),
            "unavailable": unavailable,
            "errors": errors,
        }

    # ------------------------------------------------------------------

    def new_stuff(self, *, days: int = 7, file_root: str = "Groups",
                  max_depth: int = 3) -> dict[str, Any]:
        """"Was gibt es Neues?" — the cheap poll for a notification prompt."""
        unavailable: list[str] = []
        errors: dict[str, str] = {}

        badges = self._safe(
            "badges", lambda: self.client.notifications.badges(), unavailable, errors, []
        )
        unread_mail = self._safe(
            "mail", lambda: self.client.mail.unread(limit=25), unavailable, errors, []
        )
        notifications = self._safe(
            "notifications",
            lambda: self.client.notifications.list(unread_only=True),
            unavailable, errors, [],
        )
        new_files = self._safe(
            "files",
            lambda: self.client.files.recent(days=days, path=file_root, max_depth=max_depth),
            unavailable, errors, [],
        )
        exercises = self._safe(
            "exercises", lambda: self.client.exercises.upcoming(days=days), unavailable, errors, []
        )
        events = self._safe(
            "calendar", lambda: self.client.calendar.upcoming(days), unavailable, errors, []
        )

        parts = []
        if unread_mail:
            parts.append(f"{len(unread_mail)} ungelesene Mail(s)")
        if exercises:
            parts.append(f"{len(exercises)} offene Aufgabe(n)")
        if new_files:
            parts.append(f"{len(new_files)} neue Datei(en)")
        if events:
            parts.append(f"{len(events)} Termin(e)")
        if notifications:
            parts.append(f"{len(notifications)} Benachrichtigung(en)")

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window_days": days,
            "summary": ", ".join(parts) + "." if parts else "Nichts Neues.",
            "badges": _dump(badges),
            "unread_mail": _dump(unread_mail),
            "notifications": _dump(notifications),
            "new_files": _dump(new_files),
            "open_exercises": _dump(exercises),
            "upcoming_events": _dump(events),
            "unavailable": sorted(set(unavailable)),
            "errors": errors,
        }

    # ------------------------------------------------------------------

    def homework(self, *, days: int = 14, detailed: bool = True) -> dict[str, Any]:
        """Open tasks from the exercise module, with text and attachments."""
        unavailable: list[str] = []
        errors: dict[str, str] = {}

        exercises = self._safe(
            "exercises",
            lambda: (self.client.exercises.detailed(limit=20) if detailed
                     else self.client.exercises.current()),
            unavailable, errors, [],
        )

        limit = datetime.now() + timedelta(days=days)
        upcoming = [e for e in exercises if e.deadline is None or e.deadline <= limit]
        overdue = [e for e in exercises if e.deadline and e.deadline < datetime.now()]

        return {
            "summary": (
                f"{len(upcoming)} Aufgabe(n) in den nächsten {days} Tagen"
                + (f", davon {len(overdue)} überfällig." if overdue else ".")
                if exercises else "Keine offenen Aufgaben."
            ),
            "count": len(exercises),
            "due_soon": _dump(upcoming),
            "overdue": _dump(overdue),
            "all_open": _dump(exercises),
            "unavailable": unavailable,
            "errors": errors,
        }

    # ------------------------------------------------------------------

    def files(self, *, query: str | None = None, path: str = "",
              days: int | None = None, max_depth: int = 4,
              limit: int = 50) -> dict[str, Any]:
        """Find files: by name (``query``), by recency (``days``) or just browse."""
        unavailable: list[str] = []
        errors: dict[str, str] = {}

        if query:
            entries = self._safe(
                "files",
                lambda: self.client.files.search(query, path, max_depth=max_depth, limit=limit),
                unavailable, errors, [],
            )
            summary = f"{len(entries)} Datei(en) für „{query}“ gefunden."
        elif days is not None:
            entries = self._safe(
                "files",
                lambda: self.client.files.recent(days=days, path=path or "Groups",
                                                 max_depth=max_depth, limit=limit),
                unavailable, errors, [],
            )
            summary = f"{len(entries)} Datei(en) der letzten {days} Tage."
        else:
            entries = self._safe(
                "files", lambda: self.client.files.browse(path), unavailable, errors, []
            )
            summary = f"{len(entries)} Einträge in „{path or '/'}“."

        return {
            "backend": self.client.files.backend if not unavailable else None,
            "path": path,
            "summary": summary,
            "count": len(entries),
            "entries": _dump(entries),
            "unavailable": unavailable,
            "errors": errors,
        }

    # ------------------------------------------------------------------

    def search(self, query: str, *, mail_limit: int = 15,
               file_limit: int = 25, max_depth: int = 4) -> dict[str, Any]:
        """One query across mail, files and the address book."""
        unavailable: list[str] = []
        errors: dict[str, str] = {}

        messages = self._safe(
            "mail", lambda: self.client.mail.search(query, limit=mail_limit),
            unavailable, errors, [],
        )
        files = self._safe(
            "files",
            lambda: self.client.files.search(query, max_depth=max_depth, limit=file_limit),
            unavailable, errors, [],
        )
        people = self._safe(
            "addressbook", lambda: self.client.directory.search(query), unavailable, errors, []
        )

        return {
            "query": query,
            "summary": (
                f"„{query}“: {len(messages)} Mail(s), {len(files)} Datei(en), "
                f"{len(people)} Person(en)."
            ),
            "messages": _dump(messages),
            "files": _dump(files),
            "people": _dump(people),
            "unavailable": sorted(set(unavailable)),
            "errors": errors,
        }

    # ------------------------------------------------------------------

    def agenda(self, days: int = 7) -> dict[str, Any]:
        """Calendar plus task deadlines, merged into one chronological list."""
        unavailable: list[str] = []
        errors: dict[str, str] = {}

        events = self._safe(
            "calendar", lambda: self.client.calendar.upcoming(days), unavailable, errors, []
        )
        exercises = self._safe(
            "exercises", lambda: self.client.exercises.upcoming(days=days), unavailable, errors, []
        )

        items: list[dict[str, Any]] = []
        for event in events:
            items.append({
                "kind": "event",
                "when": event.start.isoformat() if event.start else None,
                "title": event.title,
                "detail": event.location or event.description,
            })
        for exercise in exercises:
            items.append({
                "kind": "deadline",
                "when": exercise.deadline.isoformat() if exercise.deadline else None,
                "title": exercise.title,
                "detail": f"Abgabe – {exercise.url or ''}".strip(" –"),
            })
        items.sort(key=lambda i: i["when"] or "9999")

        return {
            "start": date.today().isoformat(),
            "days": days,
            "summary": (
                f"{len(events)} Termin(e) und {len(exercises)} Abgabe(n) "
                f"in den nächsten {days} Tagen."
            ),
            "items": items,
            "unavailable": sorted(set(unavailable)),
            "errors": errors,
        }

    # ------------------------------------------------------------------

    def overview(self, *, days: int = 7) -> dict[str, Any]:
        """Everything at once — the single call an assistant can start from."""
        unavailable: list[str] = []
        errors: dict[str, str] = {}

        account = self._safe("account", lambda: self.client.account, unavailable, errors, None)

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "account": account.to_dict() if account else None,
            "new": self.new_stuff(days=days),
            "inbox": self.inbox(limit=10),
            "homework": self.homework(days=days * 2, detailed=False),
            "agenda": self.agenda(days),
            "groups": _dump(
                self._safe("groups", lambda: self.client.groups(), unavailable, errors, [])
            ),
            "unavailable": sorted(set(unavailable)),
            "errors": errors,
        }
