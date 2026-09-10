"""Exception hierarchy for the IServ client."""

from __future__ import annotations


class IServError(Exception):
    """Base class for everything this package raises."""


class IServConfigError(IServError):
    """Missing or invalid configuration (domain, credentials, …)."""


class IServNetworkError(IServError):
    """Network-level failure: DNS, TLS, timeout, connection reset."""


class IServAuthError(IServError):
    """Login failed or the session was rejected."""


class IServSessionExpired(IServAuthError):
    """The server bounced us back to the login form mid-session.

    The transport catches this once per request to re-login; it only reaches
    the caller if the refresh failed too.
    """


class IServAPIError(IServError):
    """An endpoint answered with an unexpected status or body."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None,
                 body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [super().__str__()]
        if self.status is not None:
            parts.append(f"(HTTP {self.status})")
        if self.url:
            parts.append(f"at {self.url}")
        return " ".join(parts)


class IServModuleUnavailable(IServAPIError):
    """The module is not installed or not enabled for this account.

    IServ installations pick and choose their modules, and admins can hide
    them per group. Optional features raise this so the assistant layer can
    report "not available" instead of failing the whole request.
    """


class IServParseError(IServError):
    """A response could not be mapped onto the models.

    Because this is a scraper of an undocumented interface, parse failures are
    expected after IServ updates — they carry the offending snippet so the
    selector can be fixed quickly.
    """

    def __init__(self, message: str, *, snippet: str | None = None) -> None:
        super().__init__(message)
        self.snippet = snippet


class IServNotFound(IServAPIError):
    """A specific mail, file or exercise does not exist (any more)."""
