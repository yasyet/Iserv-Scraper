"""Command line interface for the IServ scraper.

Human mode (default) prints readable text; ``--format json`` prints one JSON
document and nothing else, which is what a cron job or an AI assistant wants.

    iserv probe
    iserv mail list --unread
    iserv mail read 1234 --folder INBOX
    iserv files ls "Groups/Klasse 10a"
    iserv files search Klausur --ext pdf
    iserv files get "Groups/Klasse 10a/Mathe/blatt.pdf" -o blatt.pdf
    iserv exercises list --detailed
    iserv new --days 7
    iserv --format json overview > iserv.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Sequence

from .assistant import Assistant
from .client import IServClient
from .exceptions import IServError
from .models import Model


def _to_json(value: Any) -> Any:
    if isinstance(value, Model):
        return value.to_dict()
    if isinstance(value, list):
        return [_to_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    return value


def _emit(data: Any, fmt: str, text_renderer=None) -> None:
    if fmt == "json" or text_renderer is None:
        print(json.dumps(_to_json(data), ensure_ascii=False, indent=2))
        return
    text_renderer(data)


def _print_list(items: Sequence[Any], empty: str) -> None:
    if not items:
        print(empty)
        return
    for item in items:
        print(f"  • {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iserv",
        description="Scrape IServ: mail, files (own + group), tasks, calendar, notifications.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--mail-backend", choices=("auto", "imap", "web"), default=None)
    parser.add_argument("--files-backend", choices=("auto", "webdav", "web"), default=None)

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="Show the logged-in account, groups and quota")
    sub.add_parser("probe", help="Report which IServ modules this account can reach")
    sub.add_parser("groups", help="List the groups this account belongs to")
    sub.add_parser("badges", help="Unread counters per module")
    sub.add_parser("notifications", help="Notification list")

    # ---- mail ----
    mail = sub.add_parser("mail", help="Mail").add_subparsers(dest="mail_command", required=True)
    mail.add_parser("folders", help="List mail folders")
    mail_list = mail.add_parser("list", help="List messages")
    mail_list.add_argument("--folder", default="INBOX")
    mail_list.add_argument("--limit", type=int, default=25)
    mail_list.add_argument("--offset", type=int, default=0)
    mail_list.add_argument("--unread", action="store_true")
    mail_read = mail.add_parser("read", help="Read one message")
    mail_read.add_argument("uid", type=int)
    mail_read.add_argument("--folder", default="INBOX")
    mail_search = mail.add_parser("search", help="Search messages")
    mail_search.add_argument("query")
    mail_search.add_argument("--folder", default="INBOX")
    mail_search.add_argument("--limit", type=int, default=25)
    mail_attach = mail.add_parser("attachment", help="Download an attachment")
    mail_attach.add_argument("uid", type=int)
    mail_attach.add_argument("filename")
    mail_attach.add_argument("--folder", default="INBOX")
    mail_attach.add_argument("-o", "--output", default=None)

    # ---- files ----
    files = sub.add_parser("files", help="Files").add_subparsers(dest="files_command", required=True)
    files_ls = files.add_parser("ls", help="List a directory")
    files_ls.add_argument("path", nargs="?", default="")
    files_tree = files.add_parser("tree", help="Walk a subtree")
    files_tree.add_argument("path", nargs="?", default="")
    files_tree.add_argument("--depth", type=int, default=3)
    files_search = files.add_parser("search", help="Search by file name")
    files_search.add_argument("query")
    files_search.add_argument("--path", default="")
    files_search.add_argument("--depth", type=int, default=4)
    files_search.add_argument("--ext", nargs="*", default=None)
    files_recent = files.add_parser("recent", help="Recently changed files")
    files_recent.add_argument("--days", type=int, default=7)
    files_recent.add_argument("--path", default="Groups")
    files_recent.add_argument("--depth", type=int, default=3)
    files_get = files.add_parser("get", help="Download a file")
    files_get.add_argument("path")
    files_get.add_argument("-o", "--output", default=None)
    files_put = files.add_parser("put", help="Upload a file")
    files_put.add_argument("local")
    files_put.add_argument("remote")
    files.add_parser("quota", help="Show the personal quota")

    # ---- exercises ----
    ex = sub.add_parser("exercises", help="Tasks").add_subparsers(dest="ex_command", required=True)
    ex_list = ex.add_parser("list", help="List tasks")
    ex_list.add_argument("--status", choices=("current", "past", "done"), default="current")
    ex_list.add_argument("--detailed", action="store_true")
    ex_show = ex.add_parser("show", help="Show one task")
    ex_show.add_argument("id")

    # ---- calendar ----
    cal = sub.add_parser("calendar", help="Calendar")
    cal.add_argument("--days", type=int, default=14)

    # ---- directory ----
    who = sub.add_parser("people", help="Search the address book")
    who.add_argument("query")

    # ---- assistant views ----
    new = sub.add_parser("new", help="What is new? (mail, files, tasks, events)")
    new.add_argument("--days", type=int, default=7)
    inbox = sub.add_parser("inbox", help="Mailbox digest")
    inbox.add_argument("--limit", type=int, default=15)
    inbox.add_argument("--all", action="store_true", help="Include already-read mail")
    inbox.add_argument("--preview", action="store_true", help="Fetch bodies for previews")
    hw = sub.add_parser("homework", help="Open tasks with deadlines")
    hw.add_argument("--days", type=int, default=14)
    agenda = sub.add_parser("agenda", help="Events and deadlines, merged")
    agenda.add_argument("--days", type=int, default=7)
    search = sub.add_parser("search", help="Search mail, files and people at once")
    search.add_argument("query")
    ov = sub.add_parser("overview", help="Everything at once (always JSON)")
    ov.add_argument("--days", type=int, default=7)

    return parser


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level={0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG),
        format="%(levelname)s %(name)s: %(message)s",
    )

    client = IServClient.from_env(use_cache=not args.no_cache)
    if args.mail_backend:
        client.mail.use(args.mail_backend)
    if args.files_backend:
        client.files.use(args.files_backend)

    assistant = Assistant(client)
    fmt = args.format

    try:
        command = args.command

        if command == "whoami":
            _emit(client.account, fmt, lambda a: print(
                f"{a}\n  Gruppen: {', '.join(g.name for g in a.groups) or '—'}"
                f"\n  Speicher: {a.quota_used or '?'} / {a.quota_total or '?'}"
                f" ({a.quota_percent or '?'} %)"
            ))

        elif command == "probe":
            report = client.probe()
            if fmt == "json":
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                for feature, status in report.items():
                    print(f"  {feature:<18} {status}")

        elif command == "groups":
            _emit(client.groups(), fmt, lambda g: _print_list(g, "Keine Gruppen gefunden."))

        elif command == "badges":
            _emit(client.notifications.badges(), fmt,
                  lambda b: _print_list(b, "Nichts Neues."))

        elif command == "notifications":
            _emit(client.notifications.list(), fmt,
                  lambda n: _print_list(n, "Keine Benachrichtigungen."))

        elif command == "mail":
            return _run_mail(client, args, fmt)

        elif command == "files":
            return _run_files(client, args, fmt)

        elif command == "exercises":
            if args.ex_command == "list":
                items = (client.exercises.detailed(args.status) if args.detailed
                         else client.exercises.list(args.status))
                _emit(items, fmt, lambda i: _print_list(i, "Keine Aufgaben."))
            else:
                _emit(client.exercises.get(args.id), fmt, lambda e: print(
                    f"{e}\n\n{e.description or ''}\n\n"
                    + "\n".join(f"  Anhang: {a.name}" for a in e.attachments)
                ))

        elif command == "calendar":
            _emit(client.calendar.upcoming(args.days), fmt,
                  lambda e: _print_list(e, "Keine Termine."))

        elif command == "people":
            _emit(client.directory.search(args.query), fmt,
                  lambda p: _print_list(p, "Niemanden gefunden."))

        elif command == "new":
            _emit(assistant.new_stuff(days=args.days), fmt, _print_summary_sections)

        elif command == "inbox":
            data = assistant.inbox(limit=args.limit, unread_only=not args.all,
                                   with_preview=args.preview)
            _emit(data, fmt, lambda d: (print(d["summary"]), _print_list(
                [f"{m['date'] or '?'}  {m['subject'] or '(kein Betreff)'}"
                 for m in d["messages"]], "")))

        elif command == "homework":
            _emit(assistant.homework(days=args.days), fmt, _print_summary_sections)

        elif command == "agenda":
            data = assistant.agenda(args.days)
            _emit(data, fmt, lambda d: (print(d["summary"]), _print_list(
                [f"{i['when'] or '?'}  [{i['kind']}] {i['title']}" for i in d["items"]], "")))

        elif command == "search":
            _emit(assistant.search(args.query), fmt, _print_summary_sections)

        elif command == "overview":
            print(json.dumps(_to_json(assistant.overview(days=args.days)),
                             ensure_ascii=False, indent=2))

        else:  # pragma: no cover
            raise SystemExit(f"Unknown command {command}")

    finally:
        client.logout()
        client.close()

    return 0


def _print_summary_sections(data: dict[str, Any]) -> None:
    print(data.get("summary", ""))
    for key, value in data.items():
        if key in {"summary", "errors", "unavailable", "generated_at", "backend"}:
            continue
        if isinstance(value, list) and value:
            print(f"\n{key}:")
            for entry in value[:20]:
                print(f"  • {_label(entry)}")
    if data.get("unavailable"):
        print(f"\nNicht verfügbar: {', '.join(data['unavailable'])}")
    if data.get("errors"):
        print(f"Fehler: {data['errors']}")


def _label(entry: Any) -> str:
    if not isinstance(entry, dict):
        return str(entry)
    for key in ("title", "subject", "name", "path", "module"):
        if entry.get(key):
            base = str(entry[key])
            break
    else:
        base = json.dumps(entry, ensure_ascii=False)[:100]
    when = entry.get("when") or entry.get("date") or entry.get("deadline") or entry.get("modified")
    count = entry.get("count")
    if count is not None and "module" in entry:
        return f"{base}: {count}"
    return f"{when}  {base}".strip() if when else base


def _run_mail(client: IServClient, args: argparse.Namespace, fmt: str) -> int:
    command = args.mail_command
    if command == "folders":
        _emit(client.mail.folders(with_counts=True), fmt,
              lambda f: _print_list(f, "Keine Ordner."))
    elif command == "list":
        messages = client.mail.list(args.folder, limit=args.limit, offset=args.offset,
                                    unread_only=args.unread)
        _emit(messages, fmt, lambda m: _print_list(m, "Keine Mails."))
    elif command == "read":
        message = client.mail.fetch(args.uid, args.folder)
        _emit(message, fmt, lambda m: print(
            f"Von:     {m.sender}\nAn:      {', '.join(str(r) for r in m.recipients)}\n"
            f"Datum:   {m.date}\nBetreff: {m.subject}\n"
            + (f"Anhänge: {', '.join(str(a) for a in m.attachments)}\n" if m.attachments else "")
            + f"\n{m.body_text or '(kein Text)'}"
        ))
    elif command == "search":
        messages = client.mail.search(args.query, args.folder, limit=args.limit)
        _emit(messages, fmt, lambda m: _print_list(m, "Nichts gefunden."))
    elif command == "attachment":
        data = client.mail.download_attachment(args.uid, args.filename, args.folder)
        target = args.output or args.filename
        with open(target, "wb") as handle:
            handle.write(data)
        print(f"{len(data)} Bytes -> {target}")
    return 0


def _run_files(client: IServClient, args: argparse.Namespace, fmt: str) -> int:
    command = args.files_command
    if command == "ls":
        _emit(client.files.browse(args.path), fmt, lambda e: _print_list(e, "Leer."))
    elif command == "tree":
        entries = list(client.files.walk(args.path, max_depth=args.depth))
        _emit(entries, fmt, lambda e: _print_list(e, "Leer."))
    elif command == "search":
        entries = client.files.search(args.query, args.path, max_depth=args.depth,
                                      extensions=args.ext)
        _emit(entries, fmt, lambda e: _print_list(e, "Nichts gefunden."))
    elif command == "recent":
        entries = client.files.recent(days=args.days, path=args.path, max_depth=args.depth)
        _emit(entries, fmt, lambda e: _print_list(e, "Nichts Neues."))
    elif command == "get":
        target = args.output or args.path.rsplit("/", 1)[-1]
        client.files.download_to(args.path, target)
        print(f"-> {target}")
    elif command == "put":
        client.files.upload_file(args.local, args.remote)
        print(f"{args.local} -> {args.remote}")
    elif command == "quota":
        _emit(client.files.disk_usage(), fmt, lambda q: print(
            f"Belegt: {q.get('used')} von {q.get('total')} Bytes"
        ))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except IServError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
