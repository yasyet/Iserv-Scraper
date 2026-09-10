"""The calendar module.

Endpoints behind the IServ calendar (a FullCalendar frontend):

=================================================  ============================
``GET /iserv/calendar/api/upcoming``                next events, JSON
``GET /iserv/calendar/api/eventsources``            available calendars
``GET /iserv/calendar/feed/calendar-multi``         events of every calendar
                                                    (``start``, ``end``)
``GET /iserv/calendar/feed/plugin``                 events contributed by a
                                                    plugin (``plugin``,
                                                    ``start``, ``end``)
``GET /iserv/calendar/api/lookup_event``            search (``summary``,
                                                    ``start``, ``end``)
=================================================  ============================
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from .models import CalendarEvent
from .parsing import parse_calendar_events

log = logging.getLogger(__name__)

UPCOMING = "iserv/calendar/api/upcoming"
SOURCES = "iserv/calendar/api/eventsources"
FEED_MULTI = "iserv/calendar/feed/calendar-multi"
FEED_PLUGIN = "iserv/calendar/feed/plugin"
LOOKUP = "iserv/calendar/api/lookup_event"


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    from .parsing import parse_datetime

    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Cannot interpret {value!r} as a date")
    return parsed.date()


class CalendarModule:
    def __init__(self, client) -> None:
        self.client = client

    def sources(self) -> list[dict]:
        """The calendars this account can see (own, group, plugin)."""
        return self.client.transport.get_json(SOURCES, optional=True) or []

    def events(
        self,
        start: date | datetime | str,
        end: date | datetime | str | None = None,
    ) -> list[CalendarEvent]:
        """All events between ``start`` and ``end`` (inclusive)."""
        start_date = _as_date(start)
        end_date = _as_date(end) if end is not None else start_date
        if end_date < start_date:
            start_date, end_date = end_date, start_date

        key = f"calendar:{start_date}:{end_date}"
        cached = self.client.cache.get(key)
        if cached is not None:
            return cached

        payload = self.client.transport.get_json(
            FEED_MULTI,
            optional=True,
            params={"start": start_date.isoformat(), "end": end_date.isoformat()},
        )
        events = parse_calendar_events(payload)
        self.client.cache.set(key, events)
        return events

    def upcoming(self, days: int = 14) -> list[CalendarEvent]:
        """Events in the next ``days`` days.

        Uses the dedicated ``upcoming`` endpoint when it answers and falls back
        to a normal range query otherwise.
        """
        payload = None
        try:
            payload = self.client.transport.get_json(UPCOMING, optional=True)
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            log.debug("upcoming endpoint unavailable: %s", exc)

        events = parse_calendar_events(payload) if payload else []
        if events:
            limit = datetime.now() + timedelta(days=days)
            return [e for e in events if e.start is None or e.start.replace(tzinfo=None) <= limit]

        today = date.today()
        return self.events(today, today + timedelta(days=days))

    def search(self, query: str, start: date | datetime | str,
               end: date | datetime | str) -> list[CalendarEvent]:
        """Search event titles in a date range."""
        payload = self.client.transport.get_json(
            LOOKUP,
            optional=True,
            params={
                "summary": query,
                "start": datetime.combine(_as_date(start), datetime.min.time()).isoformat() + "Z",
                "end": datetime.combine(_as_date(end), datetime.max.time()).isoformat(timespec="seconds") + "Z",
            },
        )
        return parse_calendar_events(payload)

    def plugin_events(self, plugin: str, start: date | datetime | str,
                      end: date | datetime | str) -> list[CalendarEvent]:
        """Events a calendar plugin contributes (e.g. birthdays, timetable)."""
        payload = self.client.transport.get_json(
            FEED_PLUGIN,
            optional=True,
            params={
                "plugin": plugin,
                "start": _as_date(start).isoformat(),
                "end": _as_date(end).isoformat(),
            },
        )
        return parse_calendar_events(payload, calendar=plugin)
