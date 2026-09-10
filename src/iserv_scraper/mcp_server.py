"""MCP server exposing IServ to an AI assistant.

    pip install "mcp[cli]"
    python -m iserv_scraper.mcp_server

Configuration (e.g. in an MCP client)::

    {
      "mcpServers": {
        "iserv": {
          "command": "python",
          "args": ["-m", "iserv_scraper.mcp_server"],
          "env": {"ISERV_DOMAIN": "schule.de",
                  "ISERV_USERNAME": "vorname.nachname",
                  "ISERV_PASSWORD": "…"}
        }
      }
    }

Read-only by design: nothing here sends mail, uploads or deletes. Those exist
on the client (``client.mail.send``, ``client.files.upload_file``) and can be
added as tools deliberately if you want them.
"""

from __future__ import annotations

import logging
from typing import Any

from .assistant import Assistant
from .client import IServClient
from .models import Model

log = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - optional dependency
    raise SystemExit('The MCP server needs the mcp package:  pip install "mcp[cli]"') from exc


mcp = FastMCP("iserv")

_client: IServClient | None = None


def get_client() -> IServClient:
    global _client
    if _client is None:
        _client = IServClient.from_env()
        _client.ensure_login()
    return _client


def _dump(value: Any) -> Any:
    if isinstance(value, Model):
        return value.to_dict()
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# overview / notifications
# ---------------------------------------------------------------------------


@mcp.tool()
def get_whats_new(days: int = 7) -> dict[str, Any]:
    """What is new on IServ: unread mail, new group files, open tasks, events."""
    return Assistant(get_client()).new_stuff(days=days)


@mcp.tool()
def get_overview(days: int = 7) -> dict[str, Any]:
    """Everything at once: account, new items, inbox, tasks, agenda, groups."""
    return Assistant(get_client()).overview(days=days)


@mcp.tool()
def get_badges() -> list[dict[str, Any]]:
    """Unread counters per IServ module — the cheapest "anything new?" check."""
    return _dump(get_client().notifications.badges())


# ---------------------------------------------------------------------------
# mail
# ---------------------------------------------------------------------------


@mcp.tool()
def list_mail(folder: str = "INBOX", limit: int = 20, unread_only: bool = False) -> list[dict[str, Any]]:
    """List e-mail headers of a folder, newest first."""
    return _dump(get_client().mail.list(folder, limit=limit, unread_only=unread_only))


@mcp.tool()
def read_mail(uid: int, folder: str = "INBOX") -> dict[str, Any]:
    """Read one e-mail completely, including body text and attachment names."""
    return get_client().mail.fetch(uid, folder).to_dict()


@mcp.tool()
def search_mail(query: str, folder: str = "INBOX", limit: int = 20) -> list[dict[str, Any]]:
    """Full-text search inside a mail folder."""
    return _dump(get_client().mail.search(query, folder, limit=limit))


@mcp.tool()
def list_mail_folders() -> list[dict[str, Any]]:
    """All mail folders with their unread counts."""
    return _dump(get_client().mail.folders(with_counts=True))


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


@mcp.tool()
def list_files(path: str = "") -> list[dict[str, Any]]:
    """List one directory. Use "Files" for own files, "Groups" for group files."""
    return _dump(get_client().files.browse(path))


@mcp.tool()
def search_files(query: str, path: str = "", max_depth: int = 4, limit: int = 40) -> list[dict[str, Any]]:
    """Find files whose name contains ``query``, anywhere below ``path``."""
    return _dump(get_client().files.search(query, path, max_depth=max_depth, limit=limit))


@mcp.tool()
def recent_files(days: int = 7, path: str = "Groups", limit: int = 30) -> list[dict[str, Any]]:
    """Files changed in the last ``days`` days — new material in group folders."""
    return _dump(get_client().files.recent(days=days, path=path, limit=limit))


@mcp.tool()
def read_text_file(path: str, max_chars: int = 20000) -> dict[str, Any]:
    """Download a text-like file (txt, md, csv, …) and return its content."""
    data = get_client().files.download(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    truncated = len(text) > max_chars
    return {
        "path": path,
        "bytes": len(data),
        "truncated": truncated,
        "content": text[:max_chars],
    }


@mcp.tool()
def list_groups() -> list[dict[str, Any]]:
    """The groups this account belongs to (each has its own file folder)."""
    return _dump(get_client().groups())


# ---------------------------------------------------------------------------
# tasks / calendar / people
# ---------------------------------------------------------------------------


@mcp.tool()
def get_tasks(days: int = 14, detailed: bool = True) -> dict[str, Any]:
    """Open tasks from the IServ "Aufgaben" module, with deadlines."""
    return Assistant(get_client()).homework(days=days, detailed=detailed)


@mcp.tool()
def get_agenda(days: int = 7) -> dict[str, Any]:
    """Calendar events and task deadlines merged into one chronological list."""
    return Assistant(get_client()).agenda(days)


@mcp.tool()
def search_everything(query: str) -> dict[str, Any]:
    """One query across mail, files and the address book."""
    return Assistant(get_client()).search(query)


@mcp.tool()
def find_person(query: str) -> list[dict[str, Any]]:
    """Look someone up in the school's address book."""
    return _dump(get_client().directory.search(query))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
