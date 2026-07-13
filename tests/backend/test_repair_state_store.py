from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.db.operations import OperationsStore


@pytest.mark.asyncio
async def test_negative_lookup_cache_is_case_insensitive_and_expires(
    tmp_path: Path,
) -> None:
    store = OperationsStore(tmp_path / "operations.sqlite3")
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)

    await store.cache_negative_lookup(
        release_name="Movie.Release-GROUP",
        reason="not found",
        ttl_seconds=43_200,
        now=now,
    )

    cached = await store.get_negative_lookup(
        "movie.release-group",
        now=now + timedelta(hours=6),
    )
    expired = await store.get_negative_lookup(
        "Movie.Release-GROUP",
        now=now + timedelta(hours=13),
    )

    assert cached is not None
    assert cached.release_name == "Movie.Release-GROUP"
    assert cached.reason == "not found"
    assert expired is None


@pytest.mark.asyncio
async def test_repair_state_and_repaired_counter_are_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite3"
    first = OperationsStore(database)
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)

    await first.put_repair_state(
        torrent_hash="abc123",
        release_name="Movie.Release-GROUP",
        outcome="verification_pending",
        message="recheck still running",
        retryable=True,
        now=now,
    )
    assert await first.record_repaired_once(
        torrent_hash="abc123",
        release_name="Movie.Release-GROUP",
        message="verified complete and seeding",
        now=now + timedelta(minutes=1),
    )
    assert not await first.record_repaired_once(
        torrent_hash="abc123",
        release_name="Movie.Release-GROUP",
        message="duplicate observation",
        now=now + timedelta(minutes=2),
    )
    await first.put_repair_state(
        torrent_hash="abc123",
        release_name="Movie.Release-GROUP",
        outcome="verification_pending",
        message="a stale poll must not reset repair idempotency",
        retryable=True,
        now=now + timedelta(minutes=3),
    )
    assert not await first.record_repaired_once(
        torrent_hash="abc123",
        release_name="Movie.Release-GROUP",
        message="duplicate observation after state overwrite",
        now=now + timedelta(minutes=4),
    )

    reopened = OperationsStore(database)
    states = await reopened.list_repair_states()
    counters = await reopened.get_counters()

    assert counters["repaired"] == 1
    assert states["abc123"].outcome == "fixed"
    assert states["abc123"].message == "duplicate observation after state overwrite"


@pytest.mark.asyncio
async def test_legacy_targeted_repairs_remain_discoverable_for_reconciliation(
    tmp_path: Path,
) -> None:
    store = OperationsStore(tmp_path / "operations.sqlite3")
    await store.create_job(job_id="successful", kind="repair_torrent")
    await store.update_job("successful", status="success")
    await store.create_job(job_id="failed", kind="repair_torrent")
    await store.update_job("failed", status="failed")
    await store.record_activity(
        event_type="job_started",
        message="repair torrent started",
        details={"job_id": "successful", "target": "abc123", "status": "info"},
    )
    await store.record_activity(
        event_type="job_started",
        message="repair torrent started",
        details={"job_id": "failed", "target": "not-ours", "status": "info"},
    )
    await store.record_activity(
        event_type="job_started",
        message="scan and repair started",
        details={"status": "info"},
    )

    assert await store.list_repair_targets() == {"abc123"}
