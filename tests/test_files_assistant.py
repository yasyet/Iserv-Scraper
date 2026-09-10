"""FilesModule behaviour and the assistant aggregation layer."""

import json
from datetime import datetime, timedelta

import pytest

from iserv_scraper.assistant import Assistant
from iserv_scraper.exceptions import IServModuleUnavailable
from iserv_scraper.files import FilesModule
from iserv_scraper.models import (
    Account,
    Badge,
    CalendarEvent,
    Exercise,
    FileEntry,
    FileScope,
    Group,
    MailAddress,
    MailMessage,
)


class DummyCache:
    def get(self, key):
        return None

    def set(self, key, value, ttl=None):
        pass

    def invalidate(self, prefix=None):
        pass


class DummyClient:
    """Just enough of IServClient for FilesModule."""

    def __init__(self):
        self.domain = "schule.de"
        self.username = "max"
        self._password = "secret"
        self.cache = DummyCache()

        class T:
            timeout = 10
            verify = True

        self.transport = T()


def _entry(path, *, is_dir=False, days_ago=0, size=100):
    return FileEntry(
        name=path.rsplit("/", 1)[-1],
        path=path,
        is_dir=is_dir,
        size=None if is_dir else size,
        modified=datetime.now() - timedelta(days=days_ago),
        scope=FileScope.GROUP if path.startswith("Groups/") else FileScope.OWN,
    )


class FakeWebDAV:
    def __init__(self, tree):
        self.tree = tree            # path -> list[FileEntry]

    def list(self, path="", depth=1):
        return self.tree.get(path.strip("/"), [])

    def walk(self, path="", max_depth=5):
        queue = [(path.strip("/"), 0)]
        while queue:
            current, depth = queue.pop(0)
            for entry in self.tree.get(current, []):
                yield entry
                if entry.is_dir and depth + 1 < max_depth:
                    queue.append((entry.path, depth + 1))

    def close(self):
        pass


def make_files_module(tree):
    module = FilesModule(DummyClient())
    module._webdav = FakeWebDAV(tree)
    module._backend = "webdav"
    return module


TREE = {
    "": [_entry("Files", is_dir=True), _entry("Groups", is_dir=True)],
    "Groups": [_entry("Groups/Klasse 10a", is_dir=True)],
    "Groups/Klasse 10a": [
        _entry("Groups/Klasse 10a/Mathe", is_dir=True),
        _entry("Groups/Klasse 10a/info.txt", days_ago=30),
    ],
    "Groups/Klasse 10a/Mathe": [
        _entry("Groups/Klasse 10a/Mathe/Klausur_2026.pdf", days_ago=2),
        _entry("Groups/Klasse 10a/Mathe/uebung.docx", days_ago=40),
    ],
    "Files": [_entry("Files/notizen.txt", days_ago=1)],
}


# --------------------------------------------------------------------------


def test_groups_are_derived_from_the_group_folders():
    files = make_files_module(TREE)
    assert [g.name for g in files.groups()] == ["Klasse 10a"]
    assert files.groups()[0].files_path == "Groups/Klasse 10a"


def test_own_and_group_paths():
    files = make_files_module(TREE)
    assert [e.name for e in files.own()] == ["notizen.txt"]
    assert [e.name for e in files.group_files("Klasse 10a")] == ["Mathe", "info.txt"]


def test_search_walks_the_tree_and_filters_by_extension():
    files = make_files_module(TREE)

    hits = files.search("klausur")
    assert [h.name for h in hits] == ["Klausur_2026.pdf"]

    assert files.search("uebung", extensions=["pdf"]) == []
    assert [h.name for h in files.search("uebung", extensions=["docx"])] == ["uebung.docx"]


def test_recent_only_returns_files_changed_inside_the_window():
    files = make_files_module(TREE)
    recent = files.recent(days=7, path="Groups")
    assert [r.name for r in recent] == ["Klausur_2026.pdf"]


def test_uploads_require_webdav():
    files = make_files_module(TREE)
    files._backend = "web"
    with pytest.raises(IServModuleUnavailable, match="WebDAV"):
        files.upload("Files/x.txt", b"hi")


def test_web_fallback_scrapes_the_file_manager():
    html = """
    <html><body><table>
      <tr><td><a href="/iserv/file/Groups/Klasse%2010a/Mathe">Mathe</a></td>
          <td></td><td>10.06.2026</td></tr>
      <tr><td><a href="/iserv/fs/file/Groups/Klasse%2010a/Mathe/blatt.pdf">blatt.pdf</a></td>
          <td>345 KB</td><td>10.06.2026 12:15</td></tr>
    </table></body></html>
    """

    class Transport:
        timeout = 10
        verify = True

        def html(self, path, optional=False):
            from iserv_scraper.parsing import soup_of

            return soup_of(html)

    client = DummyClient()
    client.transport = Transport()
    files = FilesModule(client)
    files._backend = "web"

    entries = files.browse("Groups/Klasse 10a")
    assert [e.name for e in entries] == ["Mathe", "blatt.pdf"]
    folder, pdf = entries
    assert folder.is_dir is True
    assert pdf.is_dir is False
    assert pdf.size == 353280
    assert pdf.scope is FileScope.GROUP and pdf.group == "Klasse 10a"
    assert pdf.download_url.endswith("/iserv/fs/file/Groups/Klasse 10a/Mathe/blatt.pdf")


# --------------------------------------------------------------------------
# assistant
# --------------------------------------------------------------------------


class StubClient:
    def __init__(self, *, broken=()):
        self._broken = set(broken)
        self.account = Account(username="max", display_name="Max Mustermann",
                               email="max@schule.de", domain="schule.de")

        outer = self

        class Mail:
            backend = "imap"

            def list(self, folder="INBOX", limit=25, offset=0, unread_only=False, search=None):
                outer._guard("mail")
                messages = [
                    MailMessage(uid=1, subject="Elternabend",
                                sender=MailAddress("Sekretariat", "s@schule.de"),
                                date=datetime.now(), seen=False),
                    MailMessage(uid=2, subject="Alt", sender=MailAddress(None, "a@schule.de"),
                                date=datetime.now() - timedelta(days=3), seen=True),
                ]
                return [m for m in messages if m.unread] if unread_only else messages

            def unread(self, folder="INBOX", limit=50):
                return self.list(folder, limit=limit, unread_only=True)

            def search(self, query, folder="INBOX", limit=50):
                return self.list(folder, limit=limit)

            def fetch(self, uid, folder="INBOX"):
                message = self.list()[0]
                message.body_text = "Der Elternabend findet am Montag statt."
                return message

        class Files:
            backend = "webdav"

            def recent(self, days=7, path="Groups", max_depth=3, limit=50):
                outer._guard("files")
                return [_entry("Groups/Klasse 10a/Mathe/Klausur_2026.pdf", days_ago=1)]

            def search(self, query, path="", max_depth=4, limit=40):
                outer._guard("files")
                return [_entry("Groups/Klasse 10a/Mathe/Klausur_2026.pdf", days_ago=1)]

            def browse(self, path=""):
                outer._guard("files")
                return [_entry("Groups", is_dir=True)]

        class Exercises:
            def upcoming(self, days=14):
                outer._guard("exercises")
                return [Exercise(id=1, title="Lineare Funktionen",
                                 deadline=datetime.now() + timedelta(days=2))]

            def current(self):
                return self.upcoming()

            def detailed(self, status="current", limit=20):
                return self.upcoming()

        class Calendar:
            def upcoming(self, days=14):
                outer._guard("calendar")
                return [CalendarEvent(id="a", title="Klassenarbeit",
                                      start=datetime.now() + timedelta(days=3), end=None)]

        class Notifications:
            def badges(self, only_nonzero=True):
                outer._guard("badges")
                return [Badge(module="mail", count=1)]

            def list(self, unread_only=False):
                outer._guard("notifications")
                return []

        class Directory:
            def search(self, query):
                outer._guard("addressbook")
                return []

        self.mail = Mail()
        self.files = Files()
        self.exercises = Exercises()
        self.calendar = Calendar()
        self.notifications = Notifications()
        self.directory = Directory()

    def _guard(self, name):
        if name in self._broken:
            raise IServModuleUnavailable(f"{name} not available")

    def groups(self):
        self._guard("groups")
        return [Group(name="Klasse 10a")]


def test_new_stuff_collects_every_source():
    data = Assistant(StubClient()).new_stuff(days=7)

    assert data["unavailable"] == []
    assert len(data["unread_mail"]) == 1
    assert len(data["new_files"]) == 1
    assert len(data["open_exercises"]) == 1
    assert len(data["upcoming_events"]) == 1
    assert "ungelesene" in data["summary"]
    json.dumps(data, ensure_ascii=False)     # must stay serialisable


def test_new_stuff_degrades_per_module():
    data = Assistant(StubClient(broken={"files", "exercises"})).new_stuff()

    assert sorted(data["unavailable"]) == ["exercises", "files"]
    assert data["errors"] == {}
    assert len(data["unread_mail"]) == 1     # working modules still delivered


def test_inbox_digest_can_fetch_previews():
    data = Assistant(StubClient()).inbox(limit=5, unread_only=True, with_preview=True)

    assert data["unread_count"] == 1
    assert data["backend"] == "imap"
    assert "Elternabend" in data["messages"][0]["preview"]


def test_agenda_merges_events_and_deadlines():
    data = Assistant(StubClient()).agenda(days=7)

    kinds = [item["kind"] for item in data["items"]]
    assert sorted(kinds) == ["deadline", "event"]
    assert data["items"][0]["when"] <= data["items"][1]["when"]   # chronological


def test_search_spans_modules():
    data = Assistant(StubClient()).search("Klausur")
    assert len(data["messages"]) == 2
    assert len(data["files"]) == 1
    assert "Klausur" in data["summary"]


def test_overview_contains_every_section():
    data = Assistant(StubClient()).overview(days=7)

    assert data["account"]["username"] == "max"
    assert set(data) >= {"new", "inbox", "homework", "agenda", "groups"}
    json.dumps(data, ensure_ascii=False)
