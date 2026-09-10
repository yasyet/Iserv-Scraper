# IServ-Scraper

Ein reverse-engineerter Python-Client für **IServ**-Schulserver – gebaut als
Datenquelle für einen KI-Assistenten.

Er liest **E-Mails (inkl. Anhänge), Dateien – eigene *und* die aller Gruppen –,
Aufgaben, Termine, Benachrichtigungen, Adressbuch und News** und gibt alles als
JSON-fähige Objekte zurück.

```python
from iserv_scraper import Assistant, IServClient

with IServClient.from_env() as iserv:
    print(Assistant(iserv).new_stuff()["summary"])
    # -> "3 ungelesene Mail(s), 2 offene Aufgabe(n), 5 neue Datei(en), 1 Termin(e)."
```

IServ hat keine offene API für Schüler-Accounts. Dieses Projekt spricht dieselben
internen Endpunkte wie die Weboberfläche – plus IMAP und WebDAV, wo IServ sie
ohnehin anbietet, weil das die stabileren Wege sind.

---

## Installation

```bash
git clone https://github.com/yasyet/Iserv-Scraper
cd Iserv-Scraper
python -m venv .venv && source .venv/bin/activate
pip install -e .          # oder: pip install -r requirements.txt
cp .env.example .env      # und ausfüllen
```

### Erster Schritt: `probe`

IServ-Installationen unterscheiden sich stark – jede Schule aktiviert andere
Module, und Admins können sie pro Gruppe ausblenden. `probe` sagt dir konkret,
was dein Account erreicht:

```bash
iserv probe
#   login              ok
#   mail_backend       ok (imap)
#   mail_folders       ok (7 entries)
#   mail_inbox         ok (5 entries)
#   files_backend      ok (webdav)
#   files_own          ok (12 entries)
#   files_groups       ok (4 entries)
#   exercises          ok (3 entries)
#   calendar           ok (6 entries)
#   badges             ok (9 entries)
#   news               unavailable
```

Alles, was `unavailable` meldet, überspringt die Assistenz-Schicht sauber, statt
einen Fehler zu werfen.

---

## Die zwei Backends

Für Mail und Dateien gibt es jeweils zwei Wege. Der Client probiert automatisch
den stabileren zuerst und fällt auf die Weboberfläche zurück.

| Bereich | bevorzugt | Fallback |
|---|---|---|
| **Mail** | **IMAPS** auf Port 993 mit denselben Zugangsdaten – vollständige Nachrichten, echte Flags, serverseitige Suche, Anhänge | die Endpunkte des Mail-Moduls (`/iserv/mail/api/…`) |
| **Dateien** | **WebDAV** unter `https://<domain>/webdav/` – `Files/` = Eigene, `Groups/<Gruppe>/` = Gruppen; inkl. Upload | der Dateimanager (`/iserv/file/…`, Downloads über `/iserv/fs/file/…`) |

Erzwingen lässt sich das jederzeit:

```bash
iserv --mail-backend web mail list
iserv --files-backend webdav files ls Groups
```
```python
iserv.mail.use("imap")
iserv.files.use("web")
```

---

## CLI

```bash
iserv whoami                       # Account, Gruppen, Speicherplatz
iserv probe
iserv groups
iserv badges                       # ungelesene Zähler pro Modul

# Mail
iserv mail folders
iserv mail list --unread --limit 20
iserv mail read 1234 --folder INBOX
iserv mail search "Klausur"
iserv mail attachment 1234 blatt.pdf -o blatt.pdf

# Dateien (eigene + Gruppen)
iserv files ls                     # Wurzel: Files/ und Groups/
iserv files ls "Groups/Klasse 10a"
iserv files tree Groups --depth 3
iserv files search Klausur --ext pdf
iserv files recent --days 7
iserv files get "Groups/Klasse 10a/Mathe/blatt.pdf" -o blatt.pdf
iserv files put ./abgabe.pdf "Files/Abgaben/abgabe.pdf"
iserv files quota

# Aufgaben, Termine, Personen
iserv exercises list --detailed
iserv exercises show 512
iserv calendar --days 14
iserv people "Mustermann"

# Assistenz-Sichten
iserv new --days 7                 # was ist neu?
iserv inbox --preview
iserv homework --days 14
iserv agenda --days 7
iserv search "Klausur"             # Mail + Dateien + Adressbuch
iserv --format json overview > iserv.json
```

Ohne Installation: `cd src && python -m iserv_scraper new`.

---

## Anbindung an den KI-Assistenten

**1. MCP-Server (empfohlen)**

```bash
pip install -e ".[mcp]"
python -m iserv_scraper.mcp_server
```

```json
{
  "mcpServers": {
    "iserv": {
      "command": "python",
      "args": ["-m", "iserv_scraper.mcp_server"],
      "env": {
        "ISERV_DOMAIN": "schule.de",
        "ISERV_USERNAME": "vorname.nachname",
        "ISERV_PASSWORD": "…"
      }
    }
  }
}
```

Tools: `get_whats_new`, `get_overview`, `get_badges`, `list_mail`, `read_mail`,
`search_mail`, `list_mail_folders`, `list_files`, `search_files`, `recent_files`,
`read_text_file`, `list_groups`, `get_tasks`, `get_agenda`, `search_everything`,
`find_person`.

Der MCP-Server ist **bewusst nur lesend**. Senden, Hochladen und Löschen gibt es
am Client (`iserv.mail.send(...)`, `iserv.files.upload_file(...)`) – wer das als
Tool will, trägt es bewusst nach.

**2. JSON über die CLI** – `iserv --format json overview` in einen Cronjob.

**3. Direkt als Bibliothek** – `Assistant(iserv).new_stuff()` liefert ein Dict,
das direkt als Tool-Result taugt.

### Die `Assistant`-Schicht

| Methode | Inhalt |
|---|---|
| `new_stuff(days)` | Badges, ungelesene Mails, neue Gruppen-Dateien, offene Aufgaben, Termine |
| `inbox(limit, with_preview)` | Postfach-Übersicht, optional mit Textvorschau |
| `homework(days)` | Aufgaben, getrennt nach „fällig demnächst" und „überfällig" |
| `agenda(days)` | Termine und Abgabefristen in einer chronologischen Liste |
| `files(query/days/path)` | Dateisuche, Neuigkeiten oder schlichtes Blättern |
| `search(query)` | eine Anfrage über Mail + Dateien + Adressbuch |
| `overview(days)` | alles auf einmal |

Jedes Ergebnis enthält `summary` (ein deutscher Satz), `unavailable` (Module, die
diese Schule nicht freigibt) und `errors`. Eine kaputte Sektion macht nie die
ganze Antwort kaputt.

---

## Bibliotheks-API

```python
from iserv_scraper import IServClient

iserv = IServClient.from_env()

# Mail
iserv.mail.folders(with_counts=True)
iserv.mail.list("INBOX", limit=25, unread_only=True)
iserv.mail.fetch(1234)                       # inkl. body_text/body_html/Anhänge
iserv.mail.search("Klassenfahrt")
iserv.mail.download_attachment(1234, "blatt.pdf")
iserv.mail.mark_read(1234)                   # nur IMAP-Backend
iserv.mail.send("lehrer@schule.de", "Abgabe", "Anbei.", attachments=["a.pdf"])

# Dateien – eigene und Gruppen
iserv.files.own()
iserv.groups()                               # jede Gruppe hat einen Dateiordner
iserv.files.group_files("Klasse 10a", "Mathe")
iserv.files.walk("Groups", max_depth=4)
iserv.files.search("Klausur", extensions=["pdf"])
iserv.files.recent(days=7)
iserv.files.download("Groups/Klasse 10a/Mathe/blatt.pdf")
iserv.files.upload_file("abgabe.pdf", "Files/Abgaben/abgabe.pdf")
iserv.files.disk_usage()

# Aufgaben, Kalender, Benachrichtigungen, Adressbuch
iserv.exercises.current(); iserv.exercises.get(512); iserv.exercises.detailed()
iserv.calendar.upcoming(14); iserv.calendar.events("2026-06-01", "2026-06-30")
iserv.notifications.badges(); iserv.notifications.list(unread_only=True)
iserv.directory.search("Mustermann"); iserv.directory.person("erika.mustermann")
```

Jedes Modell hat `to_dict()` und liefert reine JSON-Typen:

```python
entry.to_dict()
# {'name': 'blatt.pdf', 'path': 'Groups/Klasse 10a/Mathe/blatt.pdf',
#  'is_dir': False, 'size': 34567, 'modified': '2026-06-10T12:15:00',
#  'scope': 'group', 'group': 'Klasse 10a', ...}
```

---

## Reverse Engineering: die Endpunkte

Alles hier stammt aus der Weboberfläche (DevTools-Netzwerktab) bzw. aus den
Protokollen, die IServ ohnehin öffentlich anbietet.

### Anmeldung (Symfony-Formular-Login)

| Zweck | Request |
|---|---|
| Login-Seite (Cookie + `_csrf_token`) | `GET /iserv/app/login` |
| Anmelden | `POST /iserv/app/login` mit `_username`, `_password`, `_csrf_token` |
| Alternative (ältere Installationen) | `/iserv/auth/login` |
| Abmelden | `GET /iserv/app/logout?_csrf=…` |

Session-Cookies: `IServSession`, `IServSAT`, `IServSATId`.
**Wichtig:** Eine abgelaufene Session liefert kein 401, sondern einen 302 auf das
Login-Formular. Genau das erkennt `http.looks_like_login_page()` – und der
Transport meldet sich dann genau einmal automatisch neu an.

### Mail

| Zweck | Request |
|---|---|
| Ordnerliste | `GET /iserv/mail/api/folder/list` |
| Nachrichtenliste | `GET /iserv/mail/api/message/list?path=INBOX&start=0&length=50&order[column]=date&order[dir]=desc` |
| Rohnachricht (RFC 822) | `GET /iserv/mail/show/source?path=INBOX&msg=<uid>` |
| Gerenderte Nachricht | `GET /iserv/mail/show?path=…&msg=…` |
| Versand | SMTPS auf Port 465, IServ-Zugangsdaten |
| Alternative | IMAPS auf Port 993 |

Die Nachrichtenliste ist eine DataTables-Antwort; je nach Version kommt sie als
JSON oder als HTML-Seite mit dem Payload in `<script id="php-data">` – beides
wird gelesen.

### Dateien

| Zweck | Request |
|---|---|
| WebDAV-Wurzel | `PROPFIND https://<domain>/webdav/` (Basic-Auth) |
| Eigene Dateien | `…/webdav/Files/` |
| Gruppendateien | `…/webdav/Groups/<Gruppe>/` |
| Dateimanager (Fallback) | `GET /iserv/file/<pfad>` |
| Download (Fallback) | `GET /iserv/fs/file/<pfad>` |
| Ordnergröße | `GET /iserv/file/calc?path=…` |
| Speicherplatz | `GET /iserv/du/account` → `<script id="user-diskusage-data">` |

### Aufgaben

| Zweck | Request |
|---|---|
| Übersicht | `GET /iserv/exercise?filter[status]=current` (`current`/`past`/`done`) |
| Detailseite | `GET /iserv/exercise/show/<id>` |
| CSV-Export | `GET /iserv/exercise.csv` |

Reines HTML – wird mit BeautifulSoup gelesen, Anhänge über `/iserv/fs/file/…`.

### Kalender

| Zweck | Request |
|---|---|
| Kalenderquellen | `GET /iserv/calendar/api/eventsources` |
| Termine im Zeitraum | `GET /iserv/calendar/feed/calendar-multi?start=&end=` |
| Nächste Termine | `GET /iserv/calendar/api/upcoming` |
| Plugin-Termine | `GET /iserv/calendar/feed/plugin?plugin=&start=&end=` |
| Suche | `GET /iserv/calendar/api/lookup_event?summary=&start=&end=` |

### Benachrichtigungen, Personen, Sonstiges

| Zweck | Request |
|---|---|
| Benachrichtigungen | `GET /iserv/user/api/notifications` |
| Ungelesen-Zähler pro Modul | `GET /iserv/app/navigation/badges` |
| Als gelesen markieren | `POST /iserv/notification/api/v1/notifications/<id>/read` (bzw. `/readall`) |
| Adressbuch-Suche | `GET /iserv/addressbook/public?filter[search]=…` |
| Person | `GET /iserv/addressbook/public/show/<user>` |
| Autocomplete | `GET /iserv/core/autocomplete/api?type=list,mail&query=&limit=` |
| Profilbild | `GET /iserv/core/avatar/user/<user>` |
| Gruppenliste (Fallback) | `GET /iserv/profile/grouprequest/add` (`<option>`-Werte) |
| News | `GET /iserv/news` |
| Videokonferenz-Last | `GET /iserv/videoconference/api/health` |

### Verlässlichkeit

Das ist eine **undokumentierte interne Schnittstelle**. IMAP und WebDAV sind
standardisiert und ändern sich nicht; die `/iserv/...`-Pfade können mit einem
IServ-Update anders aussehen. Deshalb:

* jeder optionale Endpunkt läuft über `optional=True` → `IServModuleUnavailable`
  statt Absturz,
* HTML wird über Link-Ziele erkannt (`/iserv/fs/file/…`), nicht über CSS-Klassen,
  die sich mit jedem Theme ändern,
* `iserv probe` sagt dir jederzeit, was auf deiner Installation noch geht.

Die Endpunkte stammen aus der Weboberfläche und aus zwei öffentlichen Projekten,
die dieselbe Arbeit gemacht haben:
[Leo-Aqua/IServAPI](https://github.com/Leo-Aqua/IServAPI),
[RoRo160/iserv](https://github.com/RoRo160/iserv) und
[cns-nitroapp/iserv-api](https://github.com/cns-nitroapp/iserv-api).
WebDAV ist offiziell dokumentiert
([doku.iserv.de](https://doku.iserv.de/cookbook/external/webdav/)).

---

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

59 Tests, komplett offline: eine gefälschte `requests.Session` bedient Transport
und Login, WebDAV wird gegen eine aufgezeichnete `multistatus`-Antwort geprüft,
die HTML-Module gegen Fixtures unter `tests/fixtures/`.

## Projektstruktur

```
src/iserv_scraper/
├── client.py         Fassade: Session + alle Module, probe()
├── auth.py           Login/Logout, CSRF, Session-Cookies
├── http.py           Transport: Retries, Timeouts, Login-Redirect-Erkennung
├── mail.py           IMAP- und Web-Backend
├── files.py          Dateien: WebDAV- und Web-Backend, Suche, „was ist neu"
├── webdav.py         schlanker WebDAV-Client (PROPFIND/GET/PUT/MKCOL/DELETE)
├── exercises.py      Aufgaben-Modul (HTML)
├── calendar.py       Kalender-Feeds
├── notifications.py  Benachrichtigungen und Badges
├── directory.py      Adressbuch, News, Sonstiges
├── parsing.py        HTML/JSON -> Modelle
├── models.py         Dataclasses mit to_dict()
├── assistant.py      aggregierte Sichten für den KI-Assistenten
├── cli.py            Kommandozeile (Text/JSON)
├── mcp_server.py     MCP-Tools (nur lesend)
├── cache.py          TTL-Cache
├── config.py         .env / Umgebungsvariablen
└── exceptions.py
```

## Hinweis

Zugangsdaten stehen ausschließlich in `.env` (git-ignoriert) – sie gehen nie
irgendwohin außer an den eigenen Schulserver. Der Scraper nutzt die interne
Web-Schnittstelle mit dem eigenen Account; bitte sparsam abfragen (dafür ist der
Cache da) und die Nutzungsordnung der Schule beachten.
