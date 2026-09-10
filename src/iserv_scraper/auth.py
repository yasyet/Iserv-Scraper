"""IServ login.

Reverse-engineered flow (Symfony form login):

1. ``GET /iserv/app/login`` — sets the initial ``IServSession`` cookie and, on
   newer installations, embeds a ``_csrf_token`` in the form.
2. ``POST /iserv/app/login`` with ``_username`` / ``_password`` (+ the CSRF
   token when present). A successful login redirects to ``/iserv/`` and sets
   ``IServSAT`` / ``IServSATId``; a failed one re-renders the form with
   "Anmeldung fehlgeschlagen".
3. ``GET /iserv/`` — confirms the session and picks up the remaining cookies.

``/iserv/auth/login`` is accepted by IServ as well and is used as a fallback
for older installations.
"""

from __future__ import annotations

import logging

from .exceptions import IServAuthError
from .http import LOGIN_FAILED_MARKERS, Transport, find_csrf_token, looks_like_login_page

log = logging.getLogger(__name__)

LOGIN_PATHS = ("iserv/app/login", "iserv/auth/login")
LOGOUT_PATH = "iserv/app/logout"


class Authenticator:
    """Performs and refreshes the IServ session for one :class:`Transport`."""

    def __init__(self, transport: Transport, username: str, password: str) -> None:
        self.transport = transport
        self.username = username
        self._password = password
        self.csrf_token: str | None = None
        self.logged_in = False

    # ------------------------------------------------------------------

    def login(self) -> None:
        errors: list[str] = []
        for path in LOGIN_PATHS:
            try:
                self._login_via(path)
            except IServAuthError as exc:
                errors.append(f"{path}: {exc}")
                continue
            return
        raise IServAuthError(
            "Login failed on every known endpoint — check domain, username and "
            "password. Details: " + " | ".join(errors)
        )

    def _login_via(self, path: str) -> None:
        form = self.transport.request("GET", path, expect_login=True)
        if form.status_code >= 400:
            raise IServAuthError(f"login page returned HTTP {form.status_code}")

        token = find_csrf_token(form.text, "_csrf_token", "_token")
        payload = {"_username": self.username, "_password": self._password}
        if token:
            payload["_csrf_token"] = token

        response = self.transport.request(
            "POST",
            path,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            expect_login=True,
        )

        body = response.text if response.status_code < 400 else ""
        if any(marker in body for marker in LOGIN_FAILED_MARKERS):
            raise IServAuthError("wrong username or password")

        # Landing on the login form again means the POST did not take.
        if looks_like_login_page(response):
            raise IServAuthError("still on the login form after posting credentials")

        home = self.transport.request("GET", "iserv/", expect_login=True)
        if looks_like_login_page(home):
            raise IServAuthError("session was not established")

        self.csrf_token = find_csrf_token(home.text) or token
        self.logged_in = True
        log.info("Logged in to %s as %s", self.transport.base_url, self.username)

    # ------------------------------------------------------------------

    def logout(self) -> None:
        if not self.logged_in:
            return
        try:
            self.transport.request(
                "GET",
                LOGOUT_PATH,
                params={"_csrf": self.csrf_token} if self.csrf_token else None,
                expect_login=True,
            )
        except Exception as exc:  # noqa: BLE001 - logout is best effort
            log.debug("Logout request failed: %s", exc)
        finally:
            self.logged_in = False
            self.transport.session.cookies.clear()

    @property
    def cookies(self) -> dict[str, str]:
        """The IServ session cookies, useful for debugging a broken login."""
        jar = self.transport.session.cookies
        return {name: jar.get(name) for name in ("IServSession", "IServSAT", "IServSATId")
                if jar.get(name)}
