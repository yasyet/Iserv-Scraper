"""End-to-end demo of the IServ scraper.

    pip install -e .
    cp .env.example .env      # and fill it in
    python examples/demo.py
"""

from __future__ import annotations

from iserv_scraper import Assistant, IServClient
from iserv_scraper.exceptions import IServError, IServModuleUnavailable


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> None:
    with IServClient.from_env() as iserv:
        section("Account")
        print(f"  {iserv.account}")
        print(f"  Gruppen: {', '.join(g.name for g in iserv.account.groups) or '—'}")
        print(f"  Mail-Backend: {iserv.mail.backend} · Datei-Backend: {iserv.files.backend}")

        section("Ungelesene Mails")
        for message in iserv.mail.unread(limit=10):
            print(f"  {message}")

        section("Neue Dateien in Gruppen (7 Tage)")
        for entry in iserv.files.recent(days=7):
            print(f"  {entry}")

        section("Offene Aufgaben")
        try:
            for exercise in iserv.exercises.current():
                print(f"  {exercise}")
        except IServModuleUnavailable:
            print("  (Aufgaben-Modul für diesen Account nicht verfügbar)")

        section("Termine")
        for event in iserv.calendar.upcoming(14):
            print(f"  {event}")

        section("Kurzfassung für den Assistenten")
        print("  " + Assistant(iserv).new_stuff()["summary"])


if __name__ == "__main__":
    try:
        main()
    except IServError as exc:
        raise SystemExit(f"Fehler: {exc}")
