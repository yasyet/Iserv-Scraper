"""HTTP transport for IServ: retries, timeouts, JSON/HTML helpers, re-login.

IServ is a Symfony application, so there is no token to refresh — the session
lives in cookies (``IServSession``, ``IServSAT``, ``IServSATId``). When a
session dies, IServ does **not** answer 401: it 302-redirects to the login
form. Detecting that redirect is therefore the core job of this module.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Callable, Mapping

import requests

from .exceptions import (
    IServAPIError,
    IServModuleUnavailable,
    IServNetworkError,
    IServSessionExpired,
)

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

#: Paths that mean "you are not logged in (any more)".
_LOGIN_PATHS = ("/iserv/app/login", "/iserv/auth/login", "/iserv/login")

#: Phrases IServ shows on a failed login, in both shipped languages.
LOGIN_FAILED_MARKERS = (
    "Anmeldung fehlgeschlagen",
    "Login failed",
    "Ungültige Anmeldedaten",
    "Invalid credentials",
    "Benutzername oder Passwort",
)


def looks_like_login_page(response: requests.Response) -> bool:
    """True if this response is the login form rather than the page we asked for."""
    if any(path in response.url for path in _LOGIN_PATHS):
        return True
    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        return False
    head = response.text[:4000]
    return 'name="_username"' in head and 'name="_password"' in head


class Transport:
    """A thin wrapper around :class:`requests.Session` for one IServ host."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 0.5,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
        verify: bool | str = True,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.verify = verify
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            }
        )
        #: Set by the client; called once when a response turns out to be the
        #: login page.
        self.reauthenticate: Callable[[], None] | None = None

    # ------------------------------------------------------------------

    def url_for(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base_url + path.lstrip("/")

    def _sleep(self, attempt: int) -> None:
        delay = self.backoff * (2**attempt)
        time.sleep(delay + random.uniform(0, delay * 0.25))

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        stream: bool = False,
        allow_redirects: bool = True,
        expect_login: bool = False,
        _retry_auth: bool = True,
    ) -> requests.Response:
        """Perform one request, retrying transient failures.

        ``expect_login=True`` switches off the "we got bounced to the login
        form" detection — needed while logging in, obviously.
        """
        url = self.url_for(path)
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=self._clean(params),
                    data=data,
                    json=json_body,
                    headers=dict(headers or {}),
                    timeout=self.timeout,
                    allow_redirects=allow_redirects,
                    stream=stream,
                    verify=self.verify,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries:
                    log.debug("%s %s failed (%s); retrying", method, url, exc)
                    self._sleep(attempt)
                    continue
                raise IServNetworkError(f"{method} {url} failed: {exc}") from exc

            if response.status_code in RETRY_STATUS and attempt < self.retries:
                log.debug("%s %s -> %s; retrying", method, url, response.status_code)
                self._sleep(attempt)
                continue

            if not expect_login and not stream and looks_like_login_page(response):
                if _retry_auth and self.reauthenticate is not None:
                    log.info("Session expired — logging in again")
                    self.reauthenticate()
                    return self.request(
                        method, path, params=params, data=data, json_body=json_body,
                        headers=headers, stream=stream, allow_redirects=allow_redirects,
                        expect_login=expect_login, _retry_auth=False,
                    )
                raise IServSessionExpired(f"{method} {url} redirected to the login form")

            return response

        raise IServNetworkError(
            f"{method} {url} kept failing after {self.retries + 1} attempts"
            + (f": {last_error}" if last_error else "")
        )

    @staticmethod
    def _clean(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not params:
            return None
        return {k: v for k, v in params.items() if v is not None}

    # ------------------------------------------------------------------
    # decoding helpers
    # ------------------------------------------------------------------

    def json(self, method: str, path: str, *, optional: bool = False, **kwargs: Any) -> Any:
        """Request and decode JSON, mapping "module missing" onto an exception."""
        response = self.request(method, path, **kwargs)
        url = response.url

        if response.status_code >= 400:
            cls = (
                IServModuleUnavailable
                if optional and response.status_code in (403, 404, 405, 501)
                else IServAPIError
            )
            raise cls(
                f"Unexpected status for {method} {url}",
                status=response.status_code, url=url, body=response.text[:500],
            )

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError:
            cls = IServModuleUnavailable if optional else IServAPIError
            raise cls(
                f"Expected JSON from {url} but got "
                f"{response.headers.get('Content-Type', 'no content type')}",
                status=response.status_code, url=url, body=response.text[:500],
            ) from None

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.json("GET", path, **kwargs)

    def post_json(self, path: str, **kwargs: Any) -> Any:
        return self.json("POST", path, **kwargs)

    def html(self, path: str, *, method: str = "GET", optional: bool = False, **kwargs: Any):
        """Request a page and return it as a BeautifulSoup document."""
        from .parsing import soup_of   # imported lazily: bs4 is only needed here

        response = self.request(method, path, **kwargs)
        if response.status_code >= 400:
            cls = (
                IServModuleUnavailable
                if optional and response.status_code in (403, 404, 405, 501)
                else IServAPIError
            )
            raise cls(
                f"Unexpected status for {method} {response.url}",
                status=response.status_code, url=response.url, body=response.text[:500],
            )
        return soup_of(response.text)

    def download(self, path: str, **kwargs: Any) -> bytes:
        """Fetch a binary body (attachment, file, avatar)."""
        response = self.request("GET", path, **kwargs)
        if response.status_code >= 400:
            raise IServAPIError(
                f"Download failed for {response.url}",
                status=response.status_code, url=response.url,
            )
        return response.content

    def close(self) -> None:
        self.session.close()


def find_csrf_token(html: str, *names: str) -> str | None:
    """Pull a Symfony CSRF token out of a form.

    IServ names the field ``_csrf_token`` on the login form and ``_token`` in
    most module forms; some pages only expose it in the logout link
    (``?_csrf=…``). All three are covered.
    """
    candidates = names or ("_csrf_token", "_token", "_csrf")
    for name in candidates:
        match = re.search(
            rf'name=["\']{re.escape(name)}["\'][^>]*value=["\']([^"\']+)["\']', html
        )
        if match:
            return match.group(1)
        match = re.search(
            rf'value=["\']([^"\']+)["\'][^>]*name=["\']{re.escape(name)}["\']', html
        )
        if match:
            return match.group(1)
    match = re.search(r"[?&]_csrf=([^\"'&\s]+)", html)
    return match.group(1) if match else None
