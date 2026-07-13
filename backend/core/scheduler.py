"""APScheduler adapter for one replaceable backfill cron job."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from apscheduler.jobstores.base import JobLookupError  # type: ignore[import-untyped]

from backend.core.settings import validate_cron


class SchedulerBackend(Protocol):
    def add_job(
        self,
        function: Callable[[], Awaitable[None]],
        trigger: str,
        **kwargs: Any,
    ) -> object: ...

    def remove_job(self, job_id: str) -> object: ...


class CrowdarrrScheduler:
    JOB_ID = "backfill-scan"

    def __init__(
        self,
        *,
        scheduler: SchedulerBackend,
        backfill_callback: Callable[[], Awaitable[None]],
        timezone: str,
    ) -> None:
        self._scheduler = scheduler
        self._backfill_callback = backfill_callback
        self._timezone = timezone

    def configure_backfill(self, expression: str) -> None:
        validate_cron(expression)
        minute, hour, day, month, day_of_week = expression.split()
        self._scheduler.add_job(
            self._backfill_callback,
            "cron",
            id=self.JOB_ID,
            replace_existing=True,
            timezone=self._timezone,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        )

    def disable_backfill(self) -> None:
        try:
            self._scheduler.remove_job(self.JOB_ID)
        except JobLookupError:
            return
