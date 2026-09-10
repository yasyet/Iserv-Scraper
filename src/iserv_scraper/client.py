"""The IServ client — one object that owns the session and all modules.

    from iserv_scraper import IServClient

    with IServClient.from_env() as iserv:
        print(iserv.account)
        for message in iserv.mail.unread():
            print(message)
        for entry in iserv.files.group_files("Klasse 10a"):
            print(entry)

Everything is lazy: the first call that needs a session performs the login,
and a session that dies mid-run is re-established once by the transport.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from .auth import Authenticator
from .cache import TTLCache
from .calendar import CalendarModule
from .directory import DirectoryModule
from .exceptions import IServAPIError, IServConfigError, IServError, IServModuleUnavailable
from .exercises import ExercisesModule
from .files import FilesModule
from .http import Transport
from .mail import MailModule
from .models import Account, Group
from .notifications import NotificationsModule
from .parsing import text_of

log = logging.getLogger(__name__)


def _normalise_domain(value: str) -> str:
    """``https://schule.de/iserv/`` -> ``schule.de``."""
    domain = value.strip()
    domain = re.sub(r"^https?://", "", domain, flags=re.IGNORECASE)
    domain = domain.split("/", 1)[0]
    return domain.rstrip(".").lower()


class IServClient:
    """A logged-in connection to one IServ instance."""

    def __init__(
        self,
        domain: str,
        username: str,
        password: str,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        cache_ttl: float = 120.0,
        use_cache: bool = True,
        mail_backend: str = "auto",
        imap_host: str | None = None,
        imap_port: int = 993,
        webdav_url: str | None = None,
        verify: bool | str = True,
        session: requests.Session | None = None,
    ) -> None:
        if not domain:
            raise IServConfigError("Missing IServ domain (e.g. 'schule.de').")
        if not username:
            raise IServConfigError("Missing username.")
        if not password:
            raise IServConfigError("Missing password.")

        self.domain = _normalise_domain(domain)
        self.username = username.strip()
        self._password = password

        self.transport = Transport(
            f"https://{self.domain}/",
            timeout=timeout,
            retries=retries,
            session=session,
            verify=verify,
        )
        self.auth = Authenticator(self.transport, self.username, self._password)
        self.transport.reauthenticate = self.auth.login

        self.cache = TTLCache(ttl=cache_ttl, enabled=use_cache)

        # modules
        self.mail = MailModule(self, backend=mail_backend, imap_host=imap_host, imap_port=imap_port)
        self.files = FilesModule(self, webdav_url=webdav_url)
        self.exercises = ExercisesModule(self)
        self.calendar = CalendarModule(self)
        self.notifications = NotificationsModule(self)
        self.directory = DirectoryModule(self)

        self._account: Account | None = None

    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, **overrides: Any) -> "IServClient":
        """Build a client from ``ISERV_*`` environment variables / ``.env``."""
        from .config import Settings

        settings = Settings.from_env()
        kwargs: dict[str, Any] = {
            "domain": settings.domain,
            "username": settings.username,
            "password": settings.password,
            "timeout": settings.timeout,
            "retries": settings.retries,
            "cache_ttl": settings.cache_ttl,
            "use_cache": settings.use_cache,
            "mail_backend": settings.mail_backend,
            "imap_host": settings.imap_host,
            "imap_port": settings.imap_port,
            "webdav_url": settings.webdav_url,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------

    def login(self) -> "IServClient":
        self.auth.login()
        return self

    def ensure_login(self) -> None:
        if not self.auth.logged_in:
            self.auth.login()

    def logout(self) -> None:
        self.auth.logout()
        self.cache.invalidate()

    def close(self) -> None:
        self.mail.close()
        self.files.close()
        self.transport.close()

    def __enter__(self) -> "IServClient":
        self.ensure_login()
        return self

    def __exit__(self, *exc_info: object) -> None:
        try:
            self.logout()
        finally:
            self.close()

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    @property
    def account_email(self) -> str:
        return f"{self.username}@{self.domain}"

    @property
    def account(self) -> Account:
        """Who we are, which groups we are in and how full the quota is."""
        if self._account is None:
            self.ensure_login()
            self._account = self._load_account()
        return self._account

    def _load_account(self) -> Account:
        display_name: str | None = None
        try:
            document = self.transport.html("iserv/profile", optional=True)
            heading = document.find("h1") or document.find(class_="page-header")
            display_name = text_of(heading)
        except IServError as exc:
            log.debug("Could not read the profile page: %s", exc)

        if not display_name:
            try:
                display_name = self.directory.person(self.username).name
            except IServError:
                display_name = None

        groups: list[Group] = []
        try:
            groups = self.files.groups()
        except IServError as exc:
            log.debug("Could not list groups: %s", exc)

        quota: dict[str, int | None] = {"used": None, "total": None}
        try:
            quota = self.files.disk_usage()
        except IServError as exc:
            log.debug("Could not read the quota: %s", exc)

        return Account(
            username=self.username,
            display_name=display_name,
            email=self.account_email,
            domain=self.domain,
            groups=groups,
            quota_used=quota.get("used"),
            quota_total=quota.get("total"),
        )

    def groups(self) -> list[Group]:
        """Every group this account belongs to."""
        return self.files.groups()

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------

    def probe(self) -> dict[str, str]:
        """Report which IServ modules this account can actually reach.

        IServ installations enable different modules and admins hide them per
        group, so run this once against your own school and wire the assistant
        to whatever comes back ``ok``.
        """
        self.ensure_login()

        checks = {
            "login": lambda: self.auth.cookies or {"session": "ok"},
            "mail_backend": lambda: [self.mail.backend],
            "mail_folders": lambda: self.mail.folders(),
            "mail_inbox": lambda: self.mail.list(limit=5),
            "files_backend": lambda: [self.files.backend],
            "files_own": lambda: self.files.own(),
            "files_groups": lambda: self.files.browse("Groups"),
            "groups": lambda: self.groups(),
            "quota": lambda: [self.files.disk_usage()],
            "exercises": lambda: self.exercises.current(),
            "calendar": lambda: self.calendar.upcoming(7),
            "notifications": lambda: self.notifications.list(),
            "badges": lambda: self.notifications.badges(only_nonzero=False),
            "addressbook": lambda: self.directory.autocomplete(self.username[:3] or "a"),
            "news": lambda: self.directory.news(limit=3),
        }

        report: dict[str, str] = {}
        for name, call in checks.items():
            try:
                result = call()
            except IServModuleUnavailable:
                report[name] = "unavailable"
            except IServAPIError as exc:
                report[name] = f"error: {exc}"
            except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
                report[name] = f"error: {type(exc).__name__}: {exc}"
            else:
                if isinstance(result, list) and len(result) == 1 and not isinstance(result[0], (str, dict)):
                    report[name] = "ok"
                elif isinstance(result, list):
                    report[name] = (
                        f"ok ({result[0]})" if len(result) == 1 and isinstance(result[0], str)
                        else f"ok ({len(result)} entries)"
                    )
                else:
                    report[name] = "ok"
        return report
