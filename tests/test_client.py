"""Client/session tests — a fake requests.Session drives the whole login flow."""

import pytest
import requests

from iserv_scraper import IServClient
from iserv_scraper.exceptions import IServAuthError, IServConfigError, IServSessionExpired
from iserv_scraper.http import find_csrf_token, looks_like_login_page

LOGIN_FORM = (
    '<html><body><form method="post">'
    '<input name="_csrf_token" value="TOKEN42">'
    '<input name="_username"><input name="_password" type="password">'
    "</form></body></html>"
)
HOME = '<html><body><a href="/iserv/app/logout?_csrf=CSRF99">Abmelden</a></body></html>'


class FakeResponse(requests.Response):
    def __init__(self, url, *, status=200, body="", json_body=None, content_type=None):
        super().__init__()
        self.status_code = status
        self.url = url
        if json_body is not None:
            import json as _json

            body = _json.dumps(json_body)
            content_type = content_type or "application/json"
        self._content = body.encode("utf-8")
        self.headers["Content-Type"] = content_type or "text/html; charset=utf-8"


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("params"), kwargs.get("data")))
        for fragment, factory in self.routes.items():
            if fragment in url:
                return factory(url, kwargs)
        return FakeResponse(url, status=404, body="not found")

    def close(self):
        pass


def default_routes():
    return {
        "iserv/app/login": lambda url, kw: FakeResponse(
            "https://schule.de/iserv/" if kw.get("data") else url,
            body=HOME if kw.get("data") else LOGIN_FORM,
        ),
        "iserv/app/logout": lambda url, kw: FakeResponse(url, body="bye"),
    }


def make_client(extra=None, **kwargs):
    routes = default_routes()
    # "iserv/" is checked last so the more specific routes win
    routes.update(extra or {})
    routes["iserv/"] = lambda url, kw: FakeResponse(url, body=HOME)
    session = FakeSession(routes)
    client = IServClient(
        domain="schule.de", username="max", password="secret",
        session=session, retries=0, **kwargs,
    )
    return client, session


# --------------------------------------------------------------------------


def test_missing_configuration_is_rejected_early():
    with pytest.raises(IServConfigError):
        IServClient(domain="", username="u", password="p")
    with pytest.raises(IServConfigError):
        IServClient(domain="schule.de", username="", password="p")
    with pytest.raises(IServConfigError):
        IServClient(domain="schule.de", username="u", password="")


@pytest.mark.parametrize(
    "raw,expected",
    [("https://schule.de/iserv/", "schule.de"), ("schule.de", "schule.de"),
     ("HTTP://Schule.DE", "schule.de"), ("schule.de/iserv", "schule.de")],
)
def test_domain_is_normalised(raw, expected):
    client = IServClient(domain=raw, username="u", password="p")
    assert client.domain == expected
    assert client.account_email == f"u@{expected}"


def test_login_posts_credentials_with_the_csrf_token():
    client, session = make_client()
    client.login()

    posts = [c for c in session.calls if c[0] == "POST"]
    assert len(posts) == 1
    payload = posts[0][3]
    assert payload["_username"] == "max"
    assert payload["_password"] == "secret"
    assert payload["_csrf_token"] == "TOKEN42"
    assert client.auth.logged_in is True
    assert client.auth.csrf_token == "CSRF99"     # picked up from the logout link


def test_wrong_password_raises_auth_error():
    routes = {
        "iserv/app/login": lambda url, kw: FakeResponse(
            url, body="Anmeldung fehlgeschlagen!" if kw.get("data") else LOGIN_FORM
        ),
        "iserv/auth/login": lambda url, kw: FakeResponse(
            url, body="Anmeldung fehlgeschlagen!" if kw.get("data") else LOGIN_FORM
        ),
    }
    session = FakeSession(routes)
    client = IServClient(domain="schule.de", username="max", password="wrong",
                         session=session, retries=0)
    with pytest.raises(IServAuthError, match="Login failed on every known endpoint"):
        client.login()


def test_a_request_bounced_to_the_login_form_triggers_one_relogin():
    state = {"bounced": False}

    def badges(url, kw):
        if not state["bounced"]:
            state["bounced"] = True
            return FakeResponse("https://schule.de/iserv/app/login", body=LOGIN_FORM)
        return FakeResponse(url, json_body={"mail": 2})

    client, session = make_client({"app/navigation/badges": badges})
    client.login()
    badge_list = client.notifications.badges()

    assert [b.to_dict() for b in badge_list] == [{"module": "mail", "count": 2}]
    logins = [c for c in session.calls if c[0] == "POST" and "login" in c[1]]
    assert len(logins) == 2      # initial login + one refresh


def test_session_expiry_without_a_refresh_hook_raises():
    from iserv_scraper.http import Transport

    session = FakeSession({"": lambda url, kw: FakeResponse(
        "https://schule.de/iserv/app/login", body=LOGIN_FORM
    )})
    transport = Transport("https://schule.de/", session=session, retries=0)
    with pytest.raises(IServSessionExpired):
        transport.request("GET", "iserv/anything")


def test_notifications_and_groups_use_the_session():
    client, _ = make_client({
        "user/api/notifications": lambda url, kw: FakeResponse(url, json_body={
            "notifications": [{"id": 1, "module": "exercise", "title": "Neue Aufgabe",
                               "date": "2026-06-10T09:00:00", "read": False}]
        }),
    })
    notifications = client.notifications.list(unread_only=True)
    assert len(notifications) == 1
    assert notifications[0].module == "exercise"


def test_probe_reports_per_module_status():
    client, _ = make_client({
        "app/navigation/badges": lambda url, kw: FakeResponse(url, json_body={"mail": 1}),
    })
    client.files.use("web")      # keep the probe away from real WebDAV
    client.mail.use("web")
    report = client.probe()

    assert report["badges"].startswith("ok")
    assert report["login"] == "ok"
    # unrouted modules must degrade, never explode
    assert all(isinstance(value, str) for value in report.values())


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def test_find_csrf_token_handles_both_attribute_orders():
    assert find_csrf_token('<input name="_token" value="A1">') == "A1"
    assert find_csrf_token('<input value="B2" name="_csrf_token">') == "B2"
    assert find_csrf_token('<a href="/logout?_csrf=C3">x</a>') == "C3"
    assert find_csrf_token("<html></html>") is None


def test_login_page_detection():
    assert looks_like_login_page(FakeResponse("https://schule.de/iserv/app/login")) is True
    assert looks_like_login_page(FakeResponse("https://schule.de/iserv/", body=LOGIN_FORM)) is True
    assert looks_like_login_page(FakeResponse("https://schule.de/iserv/", body=HOME)) is False
    assert looks_like_login_page(
        FakeResponse("https://schule.de/x", json_body={"a": 1})
    ) is False
