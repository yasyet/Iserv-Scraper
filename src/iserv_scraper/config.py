"""Configuration for the IServ client (environment / ``.env``)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .exceptions import IServConfigError

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        return False


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise IServConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass
class Settings:
    domain: str
    username: str
    password: str
    timeout: float = 20.0
    retries: int = 3
    cache_ttl: float = 120.0
    use_cache: bool = True
    mail_backend: str = "auto"          # auto | imap | web
    imap_host: str | None = None
    imap_port: int = 993
    webdav_url: str | None = None

    @classmethod
    def from_env(cls, *, dotenv: bool = True) -> "Settings":
        if dotenv:
            load_dotenv()

        domain = (os.getenv("ISERV_DOMAIN") or os.getenv("ISERV_URL") or "").strip()
        username = (os.getenv("ISERV_USERNAME") or "").strip()
        password = os.getenv("ISERV_PASSWORD") or ""

        missing = [
            name
            for name, value in (
                ("ISERV_DOMAIN", domain),
                ("ISERV_USERNAME", username),
                ("ISERV_PASSWORD", password),
            )
            if not value
        ]
        if missing:
            raise IServConfigError(
                "Missing configuration: " + ", ".join(missing)
                + ". Copy .env.example to .env and fill it in."
            )

        backend = (os.getenv("ISERV_MAIL_BACKEND") or "auto").strip().lower()
        if backend not in {"auto", "imap", "web"}:
            raise IServConfigError(
                f"ISERV_MAIL_BACKEND must be auto, imap or web — got {backend!r}"
            )

        return cls(
            domain=domain,
            username=username,
            password=password,
            timeout=_env_float("ISERV_TIMEOUT", 20.0),
            retries=int(_env_float("ISERV_RETRIES", 3)),
            cache_ttl=_env_float("ISERV_CACHE_TTL", 120.0),
            use_cache=_env_bool("ISERV_CACHE", True),
            mail_backend=backend,
            imap_host=os.getenv("ISERV_IMAP_HOST") or None,
            imap_port=int(_env_float("ISERV_IMAP_PORT", 993)),
            webdav_url=os.getenv("ISERV_WEBDAV_URL") or None,
        )
