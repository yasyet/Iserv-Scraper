"""Notifications and the navigation badges.

=====================================================  =======================
``GET /iserv/user/api/notifications``                   notification list
``GET /iserv/app/navigation/badges``                    unread counter per
                                                        module
``POST /iserv/notification/api/v1/notifications/<id>/read``   mark one as read
``POST /iserv/notification/api/v1/notifications/readall``     mark all as read
=====================================================  =======================

The badges are the cheapest "is there anything new?" check there is — one
request, no scraping — which makes them ideal for an assistant that polls.
"""

from __future__ import annotations

from .models import Badge, Notification
from .parsing import parse_badges, parse_notifications

LIST = "iserv/user/api/notifications"
BADGES = "iserv/app/navigation/badges"
READ = "iserv/notification/api/v1/notifications/{id}/read"
READ_ALL = "iserv/notification/api/v1/notifications/readall"


class NotificationsModule:
    def __init__(self, client) -> None:
        self.client = client

    def list(self, *, unread_only: bool = False) -> list[Notification]:
        payload = self.client.transport.get_json(LIST, optional=True)
        notifications = parse_notifications(payload)
        if unread_only:
            return [n for n in notifications if n.read is False]
        return notifications

    def badges(self, *, only_nonzero: bool = True) -> list[Badge]:
        """Unread counters per module (Mail, Aufgaben, Dateien, …)."""
        payload = self.client.transport.get_json(BADGES, optional=True)
        badges = parse_badges(payload)
        return [b for b in badges if b.count] if only_nonzero else badges

    def mark_read(self, notification_id: int | str) -> None:
        self.client.transport.request("POST", READ.format(id=notification_id))

    def mark_all_read(self) -> None:
        self.client.transport.request("POST", READ_ALL)
