from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.core.matching import Matcher, MatchStatus, MatchStrategy
from backend.core.scan import ScanCoordinator, ScanTrigger, mode_allows_trigger
from backend.core.scheduler import CrowdarrrScheduler
from backend.core.settings import DownloadMode
from backend.crowdnfo.client import UnsupportedLookupError
from backend.db.operations import OperationsStore


class FakeMatchProvider:
    def __init__(self, release: Any | None) -> None:
        self.release = release
        self.calls: list[dict[str, str | None]] = []

    async def lookup(
        self,
        *,
        media_sha256: str | None = None,
        release_name: str | None = None,
    ) -> Any | None:
        self.calls.append({"media_sha256": media_sha256, "release_name": release_name})
        if media_sha256 is not None and release_name is None:
            raise UnsupportedLookupError("hash-only lookup is unavailable")
        return self.release


def release_lookup(*hashes: str) -> SimpleNamespace:
    return SimpleNamespace(
        release_id=42,
        release_name="Movie.2026-GROUP",
        canonical_file_hash=hashes[0] if hashes else None,
        variants=[SimpleNamespace(file_hash=value) for value in hashes[1:]],
    )


@pytest.mark.asyncio
async def test_matcher_attempts_hash_then_logs_release_name_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    media_hash = "ab" * 32
    provider = FakeMatchProvider(release_lookup(media_hash))
    matcher = Matcher(provider=provider)
    caplog.set_level(logging.INFO)

    result = await matcher.match(
        media_sha256=media_hash,
        release_name="Movie.2026-GROUP",
    )

    assert result.status is MatchStatus.HIT
    assert result.strategy is MatchStrategy.RELEASE_NAME
    assert result.hash_verified is True
    assert provider.calls == [
        {"media_sha256": media_hash, "release_name": None},
        {"media_sha256": None, "release_name": "Movie.2026-GROUP"},
    ]
    assert "hash-only" in caplog.text
    assert "release_name" in caplog.text
    assert "match" in caplog.text


@pytest.mark.asyncio
async def test_name_hit_must_match_canonical_or_variant_hash() -> None:
    requested_hash = "cd" * 32
    provider = FakeMatchProvider(release_lookup("ab" * 32, "bc" * 32, requested_hash))

    result = await Matcher(provider=provider).match(
        media_sha256=requested_hash,
        release_name="Movie.2026-GROUP",
    )

    assert result.status is MatchStatus.HIT
    assert result.hash_verified is True
    assert result.release.release_id == 42


@pytest.mark.asyncio
async def test_name_hit_with_wrong_variant_hash_is_a_retryable_miss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FakeMatchProvider(release_lookup("ab" * 32, "bc" * 32))
    caplog.set_level(logging.INFO)

    result = await Matcher(provider=provider).match(
        media_sha256="ff" * 32,
        release_name="Movie.2026-GROUP",
    )

    assert result.status is MatchStatus.MISS
    assert result.release is None
    assert result.retryable is True
    assert result.reason == "release_hash_mismatch"
    assert "retryable" in caplog.text
    assert "miss" in caplog.text


class MemoryIdempotencyStore:
    def __init__(self) -> None:
        self.completed: set[str] = set()

    async def was_completed(self, key: str) -> bool:
        return key in self.completed

    async def mark_completed(self, key: str) -> None:
        self.completed.add(key)


@pytest.mark.asyncio
async def test_scan_is_bounded_and_idempotent_across_duplicates_and_reruns() -> None:
    active = 0
    maximum_active = 0
    processed: list[str] = []

    async def process(item: SimpleNamespace) -> str:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        processed.append(item.idempotency_key)
        await asyncio.sleep(0)
        active -= 1
        return item.idempotency_key

    store = MemoryIdempotencyStore()
    coordinator = ScanCoordinator(
        processor=process,
        idempotency_store=store,
        max_concurrency=2,
    )
    items = [
        SimpleNamespace(idempotency_key="one"),
        SimpleNamespace(idempotency_key="two"),
        SimpleNamespace(idempotency_key="one"),
        SimpleNamespace(idempotency_key="three"),
    ]

    first = await coordinator.run(items)
    second = await coordinator.run(items)

    assert maximum_active <= 2
    assert sorted(processed) == ["one", "three", "two"]
    assert first.completed == 3 and first.skipped == 1
    assert second.completed == 0 and second.skipped == 4


@pytest.mark.parametrize(
    ("mode", "trigger", "allowed"),
    [
        (DownloadMode.OFF, ScanTrigger.NEW_DOWNLOAD, False),
        (DownloadMode.OFF, ScanTrigger.BACKFILL, False),
        (DownloadMode.NEW_ONLY, ScanTrigger.NEW_DOWNLOAD, True),
        (DownloadMode.NEW_ONLY, ScanTrigger.BACKFILL, False),
        (DownloadMode.NEW_AND_BACKFILL, ScanTrigger.NEW_DOWNLOAD, True),
        (DownloadMode.NEW_AND_BACKFILL, ScanTrigger.BACKFILL, True),
    ],
)
def test_download_mode_gates_new_download_and_backfill_triggers(
    mode: DownloadMode,
    trigger: ScanTrigger,
    allowed: bool,
) -> None:
    assert mode_allows_trigger(mode, trigger) is allowed


class FakeAPScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.add_calls: list[dict[str, Any]] = []

    def add_job(self, function: Any, trigger: str, **kwargs: Any) -> None:
        call = {"function": function, "trigger": trigger, **kwargs}
        self.add_calls.append(call)
        if kwargs["id"] in self.jobs and not kwargs.get("replace_existing"):
            raise AssertionError("duplicate job was not replaced")
        self.jobs[kwargs["id"]] = call

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


def test_scheduler_replaces_single_backfill_cron_job_without_duplicates() -> None:
    scheduler = FakeAPScheduler()

    async def scan_callback() -> None:
        return None

    service = CrowdarrrScheduler(
        scheduler=scheduler,
        backfill_callback=scan_callback,
        timezone="Europe/Vienna",
    )
    service.configure_backfill("0 3 * * *")
    service.configure_backfill("15 4 * * 1")

    assert list(scheduler.jobs) == ["backfill-scan"]
    assert len(scheduler.add_calls) == 2
    latest = scheduler.jobs["backfill-scan"]
    assert latest["trigger"] == "cron"
    assert latest["id"] == "backfill-scan"
    assert latest["replace_existing"] is True
    assert latest["timezone"] == "Europe/Vienna"
    assert latest["minute"] == "15"
    assert latest["hour"] == "4"
    assert latest["day"] == "*"
    assert latest["month"] == "*"
    assert latest["day_of_week"] == "1"

    service.disable_backfill()
    assert scheduler.jobs == {}


@pytest.mark.asyncio
async def test_activity_counters_and_job_state_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite"
    first = OperationsStore(database)
    await first.initialize()
    await first.record_activity(
        event_type="match_hit",
        message="Matched by release name",
        details={"strategy": "release_name"},
    )
    await first.increment_counter("fetched")
    await first.increment_counter("fetched")
    await first.increment_counter("misses")
    await first.create_job(job_id="scan-1", kind="scan_repair")
    await first.update_job(
        "scan-1",
        status="completed",
        result={"repaired": 1, "misses": 1},
    )
    await first.close()

    reopened = OperationsStore(database)
    await reopened.initialize()
    activity = await reopened.list_activity(limit=10)
    counters = await reopened.get_counters()
    job = await reopened.get_job("scan-1")
    await reopened.close()

    assert len(activity) == 1
    assert activity[0].event_type == "match_hit"
    assert activity[0].details == {"strategy": "release_name"}
    assert counters == {"fetched": 2, "misses": 1}
    assert job.kind == "scan_repair"
    assert job.status == "completed"
    assert job.result == {"repaired": 1, "misses": 1}
