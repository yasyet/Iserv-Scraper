"""Address book, news and a few odds and ends.

=====================================================  =======================
``GET /iserv/addressbook/public?filter[search]=…``      search people
``GET /iserv/addressbook/public/show/<user>``           one person's details
``GET /iserv/core/autocomplete/api``                    fast name/mail
                                                        autocomplete
                                                        (``type``, ``query``,
                                                        ``limit``)
``GET /iserv/core/avatar/user/<user>``                  profile picture
``GET /iserv/news``                                     news articles
``GET /iserv/videoconference/api/health``               conference server load
=====================================================  =======================
"""

from __future__ import annotations

import logging

from .exceptions import IServError
from .models import NewsItem, Person
from .parsing import parse_address_book, parse_news, parse_person_detail

log = logging.getLogger(__name__)

SEARCH = "iserv/addressbook/public"
SHOW = "iserv/addressbook/public/show/{user}"
AUTOCOMPLETE = "iserv/core/autocomplete/api"
AVATAR = "iserv/core/avatar/user/{user}"
NEWS = "iserv/news"
CONFERENCE_HEALTH = "iserv/videoconference/api/health"

TOO_MANY_RESULTS = (
    "Too many results",
    "Zu viele Treffer",
)


class DirectoryModule:
    def __init__(self, client) -> None:
        self.client = client

    @property
    def _base_url(self) -> str:
        return f"https://{self.client.domain}"

    # ------------------------------------------------------------------

    def search(self, query: str) -> list[Person]:
        """Search the public address book."""
        response = self.client.transport.request(
            "GET", SEARCH, params={"filter[search]": query}
        )
        if any(marker in response.text for marker in TOO_MANY_RESULTS):
            raise IServError(
                "Too many results — please narrow the search (IServ refuses to list them)."
            )
        return parse_address_book(response.text, base_url=self._base_url)

    def autocomplete(self, query: str, *, limit: int = 25,
                     types: str = "list,mail") -> list[dict]:
        """The fast autocomplete used by the compose form — names and addresses."""
        payload = self.client.transport.get_json(
            AUTOCOMPLETE,
            optional=True,
            params={"type": types, "query": query, "limit": limit},
        )
        if isinstance(payload, dict):
            return payload.get("data") or payload.get("results") or []
        return payload or []

    def person(self, username: str) -> Person:
        """One person's address book entry."""
        response = self.client.transport.request("GET", SHOW.format(user=username))
        return parse_person_detail(response.text, username=username)

    def avatar(self, username: str) -> bytes:
        return self.client.transport.download(AVATAR.format(user=username))

    # ------------------------------------------------------------------

    def news(self, *, limit: int = 20) -> list[NewsItem]:
        """Articles from the IServ news module."""
        try:
            response = self.client.transport.request("GET", NEWS)
        except IServError as exc:
            log.debug("News module unavailable: %s", exc)
            return []
        return parse_news(response.text, base_url=self._base_url)[:limit]

    def conference_health(self) -> dict:
        """Load of the videoconference server, when the module is installed."""
        return self.client.transport.get_json(CONFERENCE_HEALTH, optional=True) or {}
