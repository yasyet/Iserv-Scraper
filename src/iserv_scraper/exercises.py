"""The "Aufgaben" (exercise) module.

IServ has no JSON API here — the module is server-rendered — so these are the
pages that get scraped:

============================================  ==================================
``GET /iserv/exercise?filter[status]=current``  overview, one row per task
``GET /iserv/exercise/show/<id>``               detail: text, dates, attachments
``GET /iserv/exercise.csv``                     CSV export (when enabled)
============================================  ==================================

``filter[status]`` accepts ``current`` (laufend), ``past`` (abgelaufen) and
``done`` (erledigt).
"""

from __future__ import annotations

import csv
import io
import logging

from .exceptions import IServError, IServModuleUnavailable
from .models import Exercise, ExerciseStatus
from .parsing import parse_exercise_detail, parse_exercise_list

log = logging.getLogger(__name__)

OVERVIEW = "iserv/exercise"
DETAIL = "iserv/exercise/show/{id}"
CSV_EXPORT = "iserv/exercise.csv"


class ExercisesModule:
    def __init__(self, client) -> None:
        self.client = client

    @property
    def _base_url(self) -> str:
        return f"https://{self.client.domain}"

    # ------------------------------------------------------------------

    def list(self, status: str | ExerciseStatus = ExerciseStatus.CURRENT) -> list[Exercise]:
        """All tasks with the given status ("current", "past", "done")."""
        value = status.value if isinstance(status, ExerciseStatus) else str(status)
        key = f"exercises:{value}"
        cached = self.client.cache.get(key)
        if cached is not None:
            return cached

        response = self.client.transport.request(
            "GET", OVERVIEW, params={"filter[status]": value}
        )
        if response.status_code >= 400:
            raise IServModuleUnavailable(
                "The exercise module is not available for this account",
                status=response.status_code, url=response.url,
            )

        exercises = parse_exercise_list(
            response.text,
            status=ExerciseStatus(value) if value in {s.value for s in ExerciseStatus} else ExerciseStatus.UNKNOWN,
            base_url=self._base_url,
        )
        self.client.cache.set(key, exercises)
        return exercises

    def current(self) -> list[Exercise]:
        return self.list(ExerciseStatus.CURRENT)

    def past(self) -> list[Exercise]:
        return self.list(ExerciseStatus.PAST)

    def done(self) -> list[Exercise]:
        return self.list(ExerciseStatus.DONE)

    def get(self, exercise_id: int | str) -> Exercise:
        """Full detail page of one task, including attachments."""
        response = self.client.transport.request("GET", DETAIL.format(id=exercise_id))
        if response.status_code >= 400:
            raise IServModuleUnavailable(
                f"Exercise {exercise_id} is not readable",
                status=response.status_code, url=response.url,
            )
        return parse_exercise_detail(response.text, exercise_id, base_url=self._base_url)

    def detailed(self, status: str | ExerciseStatus = ExerciseStatus.CURRENT,
                 *, limit: int = 25) -> list[Exercise]:
        """Overview plus the detail page of each task (one request per task)."""
        results: list[Exercise] = []
        for stub in self.list(status)[:limit]:
            try:
                detail = self.get(stub.id)
            except IServError as exc:
                log.debug("Could not load exercise %s: %s", stub.id, exc)
                results.append(stub)
                continue
            detail.status = stub.status
            detail.title = detail.title or stub.title
            detail.deadline = detail.deadline or stub.deadline
            results.append(detail)
        return results

    def upcoming(self, *, days: int = 14) -> list[Exercise]:
        """Current tasks whose deadline falls within the next ``days`` days."""
        from datetime import datetime, timedelta

        limit = datetime.now() + timedelta(days=days)
        return [
            exercise
            for exercise in self.current()
            if exercise.deadline is None or exercise.deadline <= limit
        ]

    def download_attachment(self, exercise: Exercise | int | str, filename: str) -> bytes:
        """Download one attachment of a task by file name."""
        item = exercise if isinstance(exercise, Exercise) else self.get(exercise)
        for attachment in item.attachments:
            if attachment.name == filename or attachment.path.endswith(filename):
                url = attachment.download_url or attachment.path
                return self.client.transport.download(url)
        raise IServModuleUnavailable(f"Attachment {filename!r} not found in exercise {item.id}")

    def as_csv(self) -> list[dict[str, str]]:
        """The module's own CSV export, when the school enables it."""
        response = self.client.transport.request("GET", CSV_EXPORT)
        if response.status_code >= 400 or "html" in response.headers.get("Content-Type", ""):
            raise IServModuleUnavailable(
                "The exercise CSV export is not available", status=response.status_code
            )
        text = response.content.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:1024], delimiters=";,\t") if text.strip() else csv.excel
        return list(csv.DictReader(io.StringIO(text), dialect=dialect))
