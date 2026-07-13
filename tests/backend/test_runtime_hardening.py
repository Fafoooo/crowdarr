from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import aiosqlite
import httpx
import pytest

import backend.runtime as runtime_module
from backend.connectors.health import ConnectorHealth
from backend.connectors.sab import SABCompletionEvent, SABWebhookResult
from backend.core.settings import AppSettings, ConnectorSettings, DownloadMode
from backend.db.operations import OperationsStore
from backend.main import create_app
from backend.runtime import CrowdarrrRuntime, InProcessActionQueue

SAB_SECRET_HEADER = "X-Crowdarrr-SAB-Secret"


class RecordingRuntimeStore:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, object]] = {}
        self.counters: Counter[str] = Counter()
        self.activities: list[dict[str, object]] = []
        self.completed: set[str] = set()

    async def start_job(self, *, action: str, target: str | None = None) -> str:
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs[job_id] = {
            "action": action,
            "target": target,
            "status": "running",
            "detail": None,
        }
        return job_id

    async def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        detail: str | None = None,
    ) -> None:
        self.jobs[job_id].update(status=status, detail=detail)

    async def increment_counter(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    async def record_activity(
        self,
        *,
        activity_type: str,
        status: str,
        title: str,
        message: str,
        miss_id: str | None = None,
    ) -> None:
        self.activities.append(
            {
                "type": activity_type,
                "status": status,
                "title": title,
                "message": message,
                "miss_id": miss_id,
            }
        )

    async def record_miss(
        self,
        *,
        source: str,
        release_name: str,
        reason: str,
        retryable: bool,
    ) -> str:
        del source, release_name, reason, retryable
        return "miss-1"

    async def get_counters(self) -> Mapping[str, int]:
        return dict(self.counters)

    async def recent_activity(self, *, limit: int) -> list[dict[str, object]]:
        return self.activities[-limit:]

    async def was_completed(self, key: str) -> bool:
        return key in self.completed

    async def mark_completed(self, key: str) -> None:
        self.completed.add(key)


class WebhookServices:
    def __init__(self) -> None:
        self.events: list[SABCompletionEvent] = []

    async def handle_sab_completion(
        self, event: SABCompletionEvent
    ) -> dict[str, object]:
        self.events.append(event)
        return {"accepted": True}


def sab_payload(*, storage_path: str = "/data/downloads/Movie") -> dict[str, str]:
    return {
        "release_name": "Movie.2026-GROUP",
        "storage_path": storage_path,
        "category": "movies",
        "nzo_id": "SABnzbd_nzo_1",
    }


@pytest.mark.asyncio
async def test_sab_webhook_has_dedicated_secret_and_body_limit_when_api_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CROWDARRR_API_TOKEN", raising=False)
    monkeypatch.setenv("CROWDARRR_SAB_WEBHOOK_SECRET", "hook-secret")
    services = WebhookServices()
    app = create_app(services=services, api_token="")

    oversized = (
        b'{"release_name":"Movie.2026-GROUP","storage_path":"'
        + (b"x" * (64 * 1024))
        + b'","nzo_id":"SABnzbd_nzo_1"}'
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        missing = await client.post("/api/webhooks/sabnzbd", json=sab_payload())
        wrong = await client.post(
            "/api/webhooks/sabnzbd",
            json=sab_payload(),
            headers={SAB_SECRET_HEADER: "wrong-secret"},
        )
        too_large = await client.post(
            "/api/webhooks/sabnzbd",
            content=oversized,
            headers={
                SAB_SECRET_HEADER: "hook-secret",
                "Content-Type": "application/json",
            },
        )
        accepted = await client.post(
            "/api/webhooks/sabnzbd",
            json=sab_payload(),
            headers={SAB_SECRET_HEADER: "hook-secret"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert too_large.status_code == 413
    assert accepted.status_code == 200
    assert services.events == [SABCompletionEvent(**sab_payload())]


class FakeSABHistory:
    def __init__(self, event: SABCompletionEvent) -> None:
        self.event = event
        self.calls = 0

    async def list_completed(self) -> list[SABCompletionEvent]:
        self.calls += 1
        return [self.event]


class RecordingSABWebhook:
    def __init__(
        self,
        *,
        performed_actions: tuple[str, ...] | None = None,
    ) -> None:
        self.events: list[SABCompletionEvent] = []
        self.performed_actions = performed_actions

    async def handle(self, event: SABCompletionEvent) -> SABWebhookResult:
        self.events.append(event)
        return SABWebhookResult(
            accepted=True,
            actions=("fetch",),
            performed_actions=self.performed_actions,
        )


@pytest.mark.asyncio
async def test_sab_runtime_verifies_history_path_and_processes_nzo_once() -> None:
    verified = SABCompletionEvent(**sab_payload())
    forged = SABCompletionEvent(**sab_payload(storage_path="/data/downloads/forged"))
    history = FakeSABHistory(verified)
    webhook = RecordingSABWebhook()
    store = RecordingRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=AppSettings(
            download_mode=DownloadMode.NEW_ONLY,
            dry_run=False,
            sabnzbd=ConnectorSettings(
                enabled=True,
                base_url="http://sabnzbd:8080",
            ),
        ),
        store=store,
        sab_webhook=webhook,
        sab_history=history,
    )

    forged_outcome = await runtime.handle_sab_completion(forged)
    first = await runtime.handle_sab_completion(verified)
    duplicate = await runtime.handle_sab_completion(verified)

    assert forged_outcome.status == "failed"
    assert first.status == "success"
    assert duplicate.status == "skipped"
    assert webhook.events == [verified]
    assert history.calls >= 1
    assert any("SABnzbd_nzo_1" in key for key in store.completed)


@pytest.mark.asyncio
async def test_sab_runtime_does_not_count_noop_or_persist_dry_run_completion() -> None:
    event = SABCompletionEvent(**sab_payload())
    history = FakeSABHistory(event)

    noop_store = RecordingRuntimeStore()
    noop_runtime = CrowdarrrRuntime(
        settings=AppSettings(
            download_mode=DownloadMode.NEW_ONLY,
            dry_run=False,
            sabnzbd=ConnectorSettings(
                enabled=True,
                base_url="http://sabnzbd:8080",
            ),
        ),
        store=noop_store,
        sab_webhook=RecordingSABWebhook(performed_actions=()),
        sab_history=history,
    )
    noop = await noop_runtime.handle_sab_completion(event)

    assert noop.status == "skipped"
    assert noop_store.counters == {}
    assert noop_store.activities[-1]["status"] == "info"
    assert noop_store.completed

    dry_store = RecordingRuntimeStore()
    dry_runtime = CrowdarrrRuntime(
        settings=AppSettings(
            download_mode=DownloadMode.NEW_ONLY,
            dry_run=True,
            sabnzbd=ConnectorSettings(
                enabled=True,
                base_url="http://sabnzbd:8080",
            ),
        ),
        store=dry_store,
        sab_webhook=RecordingSABWebhook(performed_actions=()),
        sab_history=history,
    )
    dry = await dry_runtime.handle_sab_completion(event)

    assert dry.status == "dry_run"
    assert dry_store.counters == {}
    assert dry_store.completed == set()


class PartialContributionWebhook:
    async def handle(self, _event: SABCompletionEvent) -> SABWebhookResult:
        return SABWebhookResult(
            accepted=True,
            actions=("contribute",),
            performed_actions=("contribute",),
            warnings={"contribute": "MediaInfo upload failed"},
        )


@pytest.mark.asyncio
async def test_sab_runtime_counts_and_warns_for_partial_contribution() -> None:
    event = SABCompletionEvent(**sab_payload())
    store = RecordingRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=AppSettings(
            download_mode=DownloadMode.OFF,
            dry_run=False,
            contribute={"enabled": True},
            sabnzbd=ConnectorSettings(
                enabled=True,
                base_url="http://sabnzbd:8080",
            ),
        ),
        store=store,
        sab_webhook=PartialContributionWebhook(),
        sab_history=FakeSABHistory(event),
    )

    outcome = await runtime.handle_sab_completion(event)

    assert outcome.status == "partial"
    assert store.counters["uploaded"] == 1
    assert store.activities[-1]["status"] == "warning"
    assert "MediaInfo upload failed" in str(store.activities[-1]["message"])
    assert store.completed


class BlockingQueuedRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, Mapping[str, str], str]] = []

    async def run_queued_action(
        self,
        *,
        action: str,
        payload: Mapping[str, str],
        job_id: str,
    ) -> None:
        self.calls.append((action, payload, job_id))
        self.started.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_action_queue_has_finite_pending_capacity_and_clear_full_error() -> None:
    queue_full = getattr(runtime_module, "ActionQueueFull", None)
    assert queue_full is not None, "backend.runtime must expose ActionQueueFull"

    store = RecordingRuntimeStore()
    runtime = BlockingQueuedRuntime()
    queue = InProcessActionQueue(
        store=store,
        max_concurrency=1,
        max_pending=1,
    )
    queue.bind(runtime)  # type: ignore[arg-type]
    try:
        await queue.enqueue(action="scan_and_repair", payload={})
        await asyncio.wait_for(runtime.started.wait(), timeout=0.2)
        await queue.enqueue(
            action="repair_torrent",
            payload={"torrent_hash": "hash-one"},
        )

        with pytest.raises(queue_full, match="(?i)full"):
            await queue.enqueue(
                action="repair_torrent",
                payload={"torrent_hash": "hash-two"},
            )
    finally:
        runtime.release.set()
        await queue.close()


@pytest.mark.asyncio
async def test_action_queue_deduplicates_active_scan_and_torrent_keys() -> None:
    store = RecordingRuntimeStore()
    runtime = BlockingQueuedRuntime()
    queue = InProcessActionQueue(
        store=store,
        max_concurrency=1,
        max_pending=4,
    )
    queue.bind(runtime)  # type: ignore[arg-type]
    try:
        scan = await queue.enqueue(action="scan_and_repair", payload={})
        await asyncio.wait_for(runtime.started.wait(), timeout=0.2)
        duplicate_scan = await queue.enqueue(action="scan_and_repair", payload={})
        torrent = await queue.enqueue(
            action="repair_torrent",
            payload={"torrent_hash": "same-hash"},
        )
        duplicate_torrent = await queue.enqueue(
            action="repair_torrent",
            payload={"torrent_hash": "same-hash"},
        )
        other_torrent = await queue.enqueue(
            action="repair_torrent",
            payload={"torrent_hash": "other-hash"},
        )

        assert duplicate_scan == scan
        assert duplicate_torrent == torrent
        assert other_torrent != torrent
        assert len(store.jobs) == 3
    finally:
        runtime.release.set()
        await queue.close()


class RecordingActionQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def enqueue(self, *, action: str, payload: dict[str, str]) -> str:
        self.calls.append((action, payload))
        return "scheduled-job"


@pytest.mark.asyncio
async def test_scheduled_backfill_uses_the_same_action_queue() -> None:
    queue = RecordingActionQueue()
    runtime = CrowdarrrRuntime(
        settings=AppSettings(),
        store=RecordingRuntimeStore(),
        action_queue=queue,
    )

    outcome = await runtime.enqueue_scheduled_backfill()

    assert outcome.job_id == "scheduled-job"
    assert queue.calls == [("scan_and_repair", {})]


@pytest.mark.asyncio
async def test_initialize_reconciles_jobs_left_running_by_a_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite3"
    previous = OperationsStore(database)
    await previous.initialize()
    await previous.create_job(job_id="interrupted", kind="scan", status="running")
    await previous.create_job(job_id="finished", kind="scan", status="success")
    await previous.close()

    restarted = OperationsStore(database)
    await restarted.initialize()
    interrupted = await restarted.get_job("interrupted")
    finished = await restarted.get_job("finished")
    await restarted.close()

    assert interrupted.status == "failed"
    assert "restart" in str(interrupted.result).casefold()
    assert finished.status == "success"


class CoordinatedHealth:
    def __init__(self, name: str, started: set[str], ready: asyncio.Event) -> None:
        self.name = name
        self.started = started
        self.ready = ready

    async def healthcheck(self) -> ConnectorHealth:
        self.started.add(self.name)
        if {"crowdnfo", "qbittorrent"} <= self.started:
            self.ready.set()
        await self.ready.wait()
        return ConnectorHealth(healthy=True, version="test")


class HangingHealth:
    async def healthcheck(self) -> ConnectorHealth:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_healthchecks_run_concurrently_with_per_connector_timeout() -> None:
    started: set[str] = set()
    ready = asyncio.Event()
    runtime = CrowdarrrRuntime(
        settings=AppSettings(
            qbittorrent=ConnectorSettings(
                enabled=True,
                base_url="http://qbittorrent:8080",
            ),
            sabnzbd=ConnectorSettings(
                enabled=True,
                base_url="http://sabnzbd:8080",
            ),
        ),
        store=RecordingRuntimeStore(),
        health_connectors={
            "crowdnfo": CoordinatedHealth("crowdnfo", started, ready),
            "qbittorrent": CoordinatedHealth("qbittorrent", started, ready),
            "sabnzbd": HangingHealth(),
        },
        healthcheck_timeout=0.05,
    )

    snapshot = await asyncio.wait_for(runtime.dashboard_snapshot(), timeout=0.3)
    statuses = {connector.id: connector.status for connector in snapshot.connectors}
    messages = {connector.id: connector.message for connector in snapshot.connectors}

    assert statuses["crowdnfo"] == "healthy"
    assert statuses["qbittorrent"] == "healthy"
    assert statuses["sabnzbd"] == "unhealthy"
    assert "timeout" in messages["sabnzbd"].casefold()


@pytest.mark.asyncio
async def test_sqlite_store_uses_wal_and_is_safe_across_shared_instances(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite3"
    first = OperationsStore(database)
    second = OperationsStore(database)
    await first.initialize()
    await second.initialize()

    async with aiosqlite.connect(database) as connection:
        cursor = await connection.execute("PRAGMA journal_mode")
        journal_mode = await cursor.fetchone()
        await cursor.close()

    await asyncio.gather(
        *(first.increment_counter("shared") for _ in range(20)),
        *(second.increment_counter("shared") for _ in range(20)),
    )
    counters = await first.get_counters()
    await first.close()
    await second.close()

    assert journal_mode is not None
    assert str(journal_mode[0]).casefold() == "wal"
    assert counters["shared"] == 40
