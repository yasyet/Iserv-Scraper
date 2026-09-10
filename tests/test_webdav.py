"""WebDAV client tests — the XML parser and the path/scope mapping."""

import pytest
import requests

from iserv_scraper.exceptions import IServAuthError, IServNotFound
from iserv_scraper.models import FileScope
from iserv_scraper.webdav import WebDAVClient


class FakeResponse(requests.Response):
    def __init__(self, url, *, status=207, body=b""):
        super().__init__()
        self.status_code = status
        self.url = url
        self._content = body


class FakeSession:
    def __init__(self, response_factory):
        self.factory = response_factory
        self.auth = None
        self.verify = True
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("headers", {})))
        return self.factory(method, url, kwargs)

    def close(self):
        pass


def make_client(factory):
    session = FakeSession(factory)
    client = WebDAVClient("https://schule.de/webdav/", "max", "secret", session=session)
    return client, session


def test_list_parses_multistatus(fixture_text):
    body = fixture_text("multistatus.xml").encode("utf-8")
    client, session = make_client(lambda m, u, k: FakeResponse(u, body=body))

    entries = client.list("Groups")

    # the collection itself ("Groups") is filtered out, its children are kept
    assert [e.path for e in entries] == [
        "Groups/Klasse 10a",
        "Groups/Klasse 10a/Mathe/blatt.pdf",
    ]

    folder, pdf = entries
    assert folder.is_dir is True
    assert folder.scope is FileScope.GROUP and folder.group == "Klasse 10a"

    assert pdf.is_dir is False
    assert pdf.size == 34567
    assert pdf.content_type == "application/pdf"
    assert pdf.etag == "abc123"
    assert pdf.extension == "pdf"
    assert pdf.modified is not None and pdf.modified.year == 2026
    assert pdf.download_url.endswith("/webdav/Groups/Klasse%2010a/Mathe/blatt.pdf")

    method, _, headers = session.calls[0]
    assert method == "PROPFIND"
    assert headers["Depth"] == "1"


def test_scope_detection_for_own_files(fixture_text):
    xml = """<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/webdav/Files/notizen.txt</d:href>
        <d:propstat><d:prop>
          <d:displayname>notizen.txt</d:displayname>
          <d:resourcetype/><d:getcontentlength>12</d:getcontentlength>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
      </d:response>
    </d:multistatus>"""
    client, _ = make_client(lambda m, u, k: FakeResponse(u, body=xml.encode()))
    entry = client.list("Files")[0]
    assert entry.scope is FileScope.OWN and entry.group is None


def test_missing_path_raises_not_found():
    client, _ = make_client(lambda m, u, k: FakeResponse(u, status=404))
    with pytest.raises(IServNotFound):
        client.list("Groups/Nope")


def test_rejected_credentials_raise_auth_error():
    client, _ = make_client(lambda m, u, k: FakeResponse(u, status=401))
    with pytest.raises(IServAuthError, match="WebDAV"):
        client.list("")


def test_walk_visits_subdirectories():
    pages = {
        "https://schule.de/webdav/": b"""<?xml version="1.0"?>
          <d:multistatus xmlns:d="DAV:">
            <d:response><d:href>/webdav/Groups/</d:href><d:propstat><d:prop>
              <d:displayname>Groups</d:displayname>
              <d:resourcetype><d:collection/></d:resourcetype>
            </d:prop></d:propstat></d:response>
          </d:multistatus>""",
        "https://schule.de/webdav/Groups": b"""<?xml version="1.0"?>
          <d:multistatus xmlns:d="DAV:">
            <d:response><d:href>/webdav/Groups/a.txt</d:href><d:propstat><d:prop>
              <d:displayname>a.txt</d:displayname><d:resourcetype/>
              <d:getcontentlength>3</d:getcontentlength>
            </d:prop></d:propstat></d:response>
          </d:multistatus>""",
    }
    client, _ = make_client(lambda m, u, k: FakeResponse(u, body=pages.get(u, b"")))
    names = [e.name for e in client.walk("", max_depth=3)]
    assert names == ["Groups", "a.txt"]
