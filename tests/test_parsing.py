"""Parser tests — offline, driven by recorded IServ payload shapes."""

from datetime import datetime

import pytest

from iserv_scraper import parsing
from iserv_scraper.mail import decode_imap_utf7
from iserv_scraper.models import ExerciseStatus


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-06-16T08:30:00", datetime(2026, 6, 16, 8, 30)),
        ("16.06.2026 08:30", datetime(2026, 6, 16, 8, 30)),
        ("16.06.2026", datetime(2026, 6, 16)),
        ("2026-06-16", datetime(2026, 6, 16)),
        (None, None),
        ("", None),
        ("kein Datum", None),
    ],
)
def test_parse_datetime(raw, expected):
    assert parsing.parse_datetime(raw) == expected


def test_parse_datetime_handles_mail_headers():
    parsed = parsing.parse_datetime("Wed, 10 Jun 2026 12:15:00 +0200")
    assert parsed is not None and parsed.year == 2026 and parsed.month == 6


@pytest.mark.parametrize(
    "raw,expected",
    [("1,2 MB", 1258291), ("345 KB", 353280), (12345, 12345), ("2048", 2048),
     ("", None), (None, None)],
)
def test_parse_size(raw, expected):
    assert parsing.parse_size(raw) == expected


def test_extract_php_data():
    html = '<html><script id="php-data">({"used": 123, "total": 456})</script></html>'
    assert parsing.extract_php_data(html) == {"used": 123, "total": 456}
    assert parsing.extract_php_data("<html></html>") is None


@pytest.mark.parametrize(
    "raw,expected",
    [("INBOX", "INBOX"), ("Entw&APw-rfe", "Entwürfe"),
     ("Gel&APY-schte Objekte", "Gelöschte Objekte"), ("Sent", "Sent")],
)
def test_decode_imap_utf7(raw, expected):
    assert decode_imap_utf7(raw) == expected


# --------------------------------------------------------------------------
# mail
# --------------------------------------------------------------------------


def test_parse_mail_list():
    payload = {
        "recordsTotal": 2,
        "data": [
            {"uid": 42, "subject": "Elternabend", "from": {"personal": "Sekretariat",
             "mail": "sekretariat@schule.de"}, "date": "2026-06-10T09:00:00",
             "size": "12 KB", "flags": ["\\Seen"], "hasAttachments": True},
            {"uid": 43, "subject": "Vertretungsplan", "from": "info@schule.de",
             "date": "2026-06-11T07:30:00", "unseen": True},
        ],
    }
    messages = parsing.parse_mail_list(payload)

    assert [m.uid for m in messages] == [43, 42]      # newest first
    newest = messages[0]
    assert newest.subject == "Vertretungsplan"
    assert newest.sender.address == "info@schule.de"
    assert newest.unread is True

    older = messages[1]
    assert older.sender.name == "Sekretariat"
    assert older.seen is True and older.unread is False
    assert older.has_attachments is True
    assert older.size == 12288


def test_parse_mail_list_survives_junk():
    assert parsing.parse_mail_list(None) == []
    assert parsing.parse_mail_list({"data": []}) == []
    assert parsing.parse_mail_list({"data": ["nonsense", 5]}) == []


def test_parse_mail_folders_flattens_children():
    payload = {"folders": [
        {"path": "INBOX", "name": "Posteingang", "unseen": 3, "total": 120,
         "children": [{"path": "INBOX.Schule", "name": "Schule", "unseen": 1, "total": 8}]},
        {"path": "Sent", "name": "Gesendet", "specialUse": "Sent"},
    ]}
    folders = parsing.parse_mail_folders(payload)
    paths = [f.path for f in folders]

    assert "INBOX" in paths and "INBOX.Schule" in paths and "Sent" in paths
    inbox = next(f for f in folders if f.path == "INBOX")
    assert inbox.unread == 3 and inbox.total == 120


def test_parse_rfc822_extracts_bodies_and_attachments():
    raw = (
        "From: Lehrer <lehrer@schule.de>\r\n"
        "To: max.mustermann@schule.de\r\n"
        "Subject: Hausaufgabe\r\n"
        "Date: Wed, 10 Jun 2026 12:15:00 +0200\r\n"
        "Message-ID: <abc@schule.de>\r\n"
        'Content-Type: multipart/mixed; boundary="BOUND"\r\n'
        "MIME-Version: 1.0\r\n"
        "\r\n"
        "--BOUND\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Bitte bis Freitag bearbeiten.\r\n"
        "--BOUND\r\n"
        'Content-Type: application/pdf; name="blatt.pdf"\r\n'
        "Content-Transfer-Encoding: base64\r\n"
        'Content-Disposition: attachment; filename="blatt.pdf"\r\n'
        "\r\n"
        "JVBERi0xLjQK\r\n"
        "--BOUND--\r\n"
    )
    message = parsing.parse_rfc822(raw, uid=7)

    assert message.subject == "Hausaufgabe"
    assert message.sender.name == "Lehrer"
    assert message.sender.address == "lehrer@schule.de"
    assert "Freitag" in message.body_text
    assert message.has_attachments is True
    assert message.attachments[0].filename == "blatt.pdf"
    assert message.attachments[0].content_type == "application/pdf"
    assert message.preview(10) == "Bitte bis…"


# --------------------------------------------------------------------------
# notifications / calendar
# --------------------------------------------------------------------------


def test_parse_badges():
    assert [b.to_dict() for b in parsing.parse_badges({"mail": 3, "exercise": 1, "files": 0})] == [
        {"module": "exercise", "count": 1},
        {"module": "files", "count": 0},
        {"module": "mail", "count": 3},
    ]
    assert parsing.parse_badges({"badges": {"mail": {"count": 2}}})[0].count == 2
    assert parsing.parse_badges(None) == []


def test_parse_notifications_sorts_newest_first():
    payload = {"notifications": [
        {"id": 1, "module": "mail", "title": "Alt", "date": "2026-06-01T10:00:00", "read": True},
        {"id": 2, "module": "exercise", "title": "Neu", "date": "2026-06-05T10:00:00", "read": False},
    ]}
    notifications = parsing.parse_notifications(payload)
    assert [n.title for n in notifications] == ["Neu", "Alt"]
    assert notifications[0].read is False


def test_parse_calendar_events():
    payload = [
        {"id": "a", "title": "Klassenarbeit", "start": "2026-06-17T08:00:00",
         "end": "2026-06-17T09:30:00", "location": "Raum 201"},
        {"id": "b", "title": "Ferienbeginn", "start": "2026-07-01", "allDay": True},
    ]
    events = parsing.parse_calendar_events(payload)
    assert [e.title for e in events] == ["Klassenarbeit", "Ferienbeginn"]
    assert events[0].location == "Raum 201"
    assert events[1].all_day is True


# --------------------------------------------------------------------------
# HTML modules
# --------------------------------------------------------------------------


def test_parse_exercise_list(fixture_text):
    exercises = parsing.parse_exercise_list(
        fixture_text("exercise_list.html"),
        status=ExerciseStatus.CURRENT,
        base_url="https://schule.de",
    )
    assert [e.id for e in exercises] == [512, 513]
    assert exercises[0].title == "Lineare Funktionen"
    assert exercises[0].deadline == datetime(2026, 6, 17, 23, 59)
    assert exercises[0].url == "https://schule.de/iserv/exercise/show/512"
    assert exercises[1].submitted is True


def test_parse_exercise_detail(fixture_text):
    exercise = parsing.parse_exercise_detail(
        fixture_text("exercise_detail.html"), 512, base_url="https://schule.de"
    )
    assert exercise.title == "Lineare Funktionen"
    assert exercise.start == datetime(2026, 6, 10, 8, 0)
    assert exercise.deadline == datetime(2026, 6, 17, 23, 59)
    assert "Seite 42" in exercise.description
    assert exercise.attachments[0].name == "blatt.pdf"
    assert exercise.attachments[0].download_url.startswith("https://schule.de/iserv/fs/file/")


def test_parse_address_book(fixture_text):
    people = parsing.parse_address_book(fixture_text("addressbook.html"),
                                        base_url="https://schule.de")
    assert [p.username for p in people] == ["erika.mustermann", "max.mustermann"]
    assert people[0].name == "Erika Mustermann"
    assert people[0].email == "erika.mustermann@schule.de"
