from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import backend.runtime as runtime_module
from backend.connectors.qbit import TorrentSnapshot
from backend.connectors.sab import SABCompletionEvent, SABLiveActionResult
from backend.core.files import WriteDisposition, WriteResult
from backend.core.hashing import HashResult
from backend.core.settings import (
    AppSettings,
    ContributionSettings,
    PathMappingSetting,
)
from backend.crowdnfo.client import UnsupportedLookupError
from backend.db.operations import OperationsStore
from backend.runtime import (
    InProcessActionQueue,
    OperationsRuntimeStore,
    QBitCompletedPoller,
    QBitLiveWorkflow,
    SABLiveWorkflow,
    StrategyAwareCrowdNFODownloader,
)


class RecordingDownloader:
    def __init__(self, payload: bytes = b"raw-nfo\r\n") -> None:
        self.payload = payload
        self.calls: list[tuple[str, str | None]] = []

    async def download_nfo(
        self,
        *,
        release_name: str,
        media_sha256: str | None = None,
    ) -> bytes:
        self.calls.append((release_name, media_sha256))
        return self.payload


@pytest.mark.asyncio
async def test_strategy_aware_downloader_enforces_all_matching_modes() -> None:
    client = RecordingDownloader()
    with pytest.raises(ValueError, match="matching mode"):
        StrategyAwareCrowdNFODownloader(client=client, mode="invented")

    names = StrategyAwareCrowdNFODownloader(
        client=client,
        mode="release_name_only",
    )
    assert (
        await names.download_nfo(
            release_name="Movie-GROUP",
            media_sha256="a" * 64,
        )
        == client.payload
    )
    assert client.calls == [("Movie-GROUP", None)]

    hashes = StrategyAwareCrowdNFODownloader(client=client, mode="hash_only")
    with pytest.raises(LookupError, match="unavailable"):
        await hashes.download_nfo(release_name="Movie-GROUP")
    with pytest.raises(UnsupportedLookupError, match="not available"):
        await hashes.download_nfo(
            release_name="Movie-GROUP",
            media_sha256="b" * 64,
        )

    fallback = StrategyAwareCrowdNFODownloader(
        client=client,
        mode="hash_then_release_name",
    )
    await fallback.download_nfo(
        release_name="Other-GROUP",
        media_sha256="c" * 64,
    )
    assert client.calls[-1] == ("Other-GROUP", "c" * 64)


@pytest.mark.parametrize(
    ("successes", "failures", "dry_runs", "expected"),
    [
        (1, 1, 0, "partial"),
        (0, 1, 0, "failed"),
        (0, 0, 1, "dry_run"),
        (1, 0, 0, "success"),
        (0, 0, 0, "skipped"),
    ],
)
def test_repair_summary_status_matrix(
    successes: int,
    failures: int,
    dry_runs: int,
    expected: str,
) -> None:
    summary = runtime_module._RepairSummary(  # noqa: SLF001
        successes=successes,
        failures=failures,
        dry_runs=dry_runs,
    )
    assert summary.status == expected


@pytest.mark.asyncio
async def test_operations_runtime_store_round_trip(tmp_path: Path) -> None:
    operations = OperationsStore(tmp_path / "operations.sqlite3")
    await operations.initialize()
    store = OperationsRuntimeStore(operations)
    try:
        job_id = await store.start_job(action="repair_torrent", target="deadbeef")
        await store.finish_job(job_id, status="success", detail="done")
        assert await store.increment_counter("fetched", 2) == 2
        await store.record_activity(
            activity_type="repair",
            status="success",
            title="Repaired",
            message="Movie-GROUP",
            miss_id="miss-explicit",
        )
        miss_id = await store.record_miss(
            source="qbittorrent",
            release_name="Missing-GROUP",
            reason="not found",
            retryable=True,
        )
        assert miss_id.startswith("miss-")
        assert (await store.get_counters())["fetched"] == 2
        activity = await store.recent_activity(limit=10)
        assert {item["type"] for item in activity} >= {
            "job_started",
            "repair",
            "miss",
        }
        assert any(item.get("miss_id") == "miss-explicit" for item in activity)
        assert await store.was_completed("event-key") is False
        await store.mark_completed("event-key")
        assert await store.was_completed("event-key") is True
    finally:
        await operations.close()


class FixedPathMapper:
    def __init__(self, path: Path) -> None:
        self.path = path

    def map_path(self, reported_path: str) -> Path:
        assert reported_path.startswith("/data/")
        return self.path


class RecordingHasher:
    def __init__(self, result: HashResult | Exception) -> None:
        self.result = result
        self.paths: list[Path] = []

    async def hash_file(self, path: Path) -> HashResult:
        self.paths.append(path)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RecordingContribution:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, bool]]] = []

    async def contribute(self, item: Any, **options: bool) -> str:
        self.calls.append((item, options))
        return "uploaded"


def live_settings(root: Path, *, dry_run: bool = False) -> AppSettings:
    return AppSettings(
        dry_run=dry_run,
        contribute=ContributionSettings(enabled=True),
        path_mappings=[PathMappingSetting(remote_root="/data", local_root=str(root))],
    )


@pytest.mark.asyncio
async def test_sab_live_workflow_fetches_and_contributes_from_release_tree(
    tmp_path: Path,
) -> None:
    release = tmp_path / "Movie-GROUP"
    release.mkdir()
    media = release / "Movie.mkv"
    media.write_bytes(b"media")
    empty_nfo = release / "Movie.nfo"
    empty_nfo.write_bytes(b"")
    (release / "proof.txt").write_text("proof")
    downloader = RecordingDownloader()
    contribution = RecordingContribution()
    hasher = RecordingHasher(HashResult("d" * 64, 5, False))
    workflow = SABLiveWorkflow(
        settings=live_settings(tmp_path),
        path_mapper=FixedPathMapper(release),
        crowdnfo=downloader,
        contribution=contribution,
        hash_service=hasher,
    )
    event = SABCompletionEvent(
        release_name="Movie-GROUP",
        storage_path="/data/Movie-GROUP",
        category="movies",
        nzo_id="nzo-1",
    )

    fetch_result = await workflow.fetch_missing(event)
    assert isinstance(fetch_result, SABLiveActionResult)
    assert fetch_result.performed is True
    assert isinstance(fetch_result.value, WriteResult)
    assert fetch_result.value.disposition is WriteDisposition.WRITTEN
    assert empty_nfo.read_bytes() == downloader.payload
    assert downloader.calls == [("Movie-GROUP", "d" * 64)]
    existing = await workflow.fetch_missing(event)
    assert existing == SABLiveActionResult(performed=False, value=empty_nfo)
    contribution_result = await workflow.contribute(event)
    assert contribution_result == SABLiveActionResult(
        performed=True,
        value="uploaded",
    )
    item, options = contribution.calls[0]
    assert item.media_path == media
    assert item.nfo_path == empty_nfo
    assert item.media_sha256 == "d" * 64
    assert {entry["file_path"] for entry in item.filelist} == {
        "Movie.mkv",
        "Movie.nfo",
        "proof.txt",
    }
    assert options == {
        "include_nfo": True,
        "include_mediainfo": True,
        "include_filelist": True,
    }


@pytest.mark.asyncio
async def test_sab_live_workflow_handles_dry_run_missing_media_and_hash_modes(
    tmp_path: Path,
) -> None:
    release = tmp_path / "Empty-GROUP"
    release.mkdir()
    event = SABCompletionEvent("Empty-GROUP", "/data/Empty-GROUP")
    dry = SABLiveWorkflow(
        settings=live_settings(tmp_path, dry_run=True),
        path_mapper=FixedPathMapper(release),
        crowdnfo=RecordingDownloader(),
        contribution=RecordingContribution(),
    )
    with pytest.raises(FileNotFoundError, match="no media"):
        await dry.fetch_missing(event)

    media = release / "Empty.mkv"
    media.write_bytes(b"video")
    assert await dry.fetch_missing(event) == SABLiveActionResult(
        performed=False,
        terminal=False,
        value=release / "Empty.nfo",
    )
    assert await dry.contribute(event) == SABLiveActionResult(
        performed=False,
        terminal=False,
    )

    hash_only = live_settings(tmp_path)
    hash_only.match_strategy = "hash_only"
    workflow = SABLiveWorkflow(
        settings=hash_only,
        path_mapper=FixedPathMapper(media),
        crowdnfo=RecordingDownloader(),
        contribution=RecordingContribution(),
        hash_service=RecordingHasher(HashResult(None, 0, False, "too large")),
    )
    with pytest.raises(LookupError, match="too large"):
        await workflow.fetch_missing(event)


def completed_torrent(
    torrent_hash: str,
    *,
    progress: float = 1.0,
    state: str = "uploading",
) -> TorrentSnapshot:
    return TorrentSnapshot(
        torrent_hash=torrent_hash,
        name=f"Release-{torrent_hash}",
        category="movies",
        content_path=f"/data/{torrent_hash}",
        progress=progress,
        state=state,
    )


class PollQBit:
    def __init__(self, torrents: list[TorrentSnapshot] | Exception) -> None:
        self.torrents = torrents

    async def list_torrents(self) -> list[TorrentSnapshot]:
        if isinstance(self.torrents, Exception):
            raise self.torrents
        return self.torrents


class CompletionMemory:
    def __init__(self) -> None:
        self.completed: set[str] = set()
        self.counters: dict[str, int] = {}
        self.activities: list[tuple[str, str, dict[str, object]]] = []

    async def was_completed(self, key: str) -> bool:
        return key in self.completed

    async def mark_completed(self, key: str) -> None:
        self.completed.add(key)

    async def increment_counter(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    async def record_activity(
        self,
        *,
        event_type: str,
        message: str,
        details: dict[str, object],
    ) -> None:
        self.activities.append((event_type, message, details))


class PollLiveService:
    def __init__(
        self,
        *,
        fail_contribution: bool = False,
        fetch_performed: bool = True,
        contribution_performed: bool = True,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_contribution = fail_contribution
        self.fetch_performed = fetch_performed
        self.contribution_performed = contribution_performed

    async def fetch_missing(self, torrent: TorrentSnapshot) -> object:
        self.calls.append(("fetch", torrent.torrent_hash))
        if self.fetch_performed:
            return WriteResult(Path("release.nfo"), WriteDisposition.WRITTEN)
        return Path("existing.nfo")

    async def contribute(self, torrent: TorrentSnapshot) -> object | None:
        self.calls.append(("contribute", torrent.torrent_hash))
        if self.fail_contribution:
            raise RuntimeError("secret=must-not-leak")
        return object() if self.contribution_performed else None


@pytest.mark.asyncio
async def test_qbit_completion_poller_filters_retries_and_marks_success() -> None:
    with pytest.raises(ValueError, match="positive"):
        QBitCompletedPoller(
            qbit=PollQBit([]),
            live_service=PollLiveService(),
            store=CompletionMemory(),
            fetch_enabled=True,
            contribute_enabled=True,
            poll_interval=0,
        )

    store = CompletionMemory()
    await store.mark_completed("qbit-completion:already")
    live = PollLiveService(fail_contribution=True)
    poller = QBitCompletedPoller(
        qbit=PollQBit(
            [
                completed_torrent("incomplete", progress=0.5),
                completed_torrent("checking", state="checkingUP"),
                completed_torrent("already"),
                completed_torrent("new"),
            ]
        ),
        live_service=live,
        store=store,
        fetch_enabled=True,
        contribute_enabled=True,
        poll_interval=0.01,
    )
    result = await poller.poll_once()
    assert tuple(result) == ("new",)
    assert result["new"].actions == ("fetch", "contribute")
    assert result["new"].errors["contribute"] == "operation failed"
    assert "qbit-completion:new" not in store.completed

    live.fail_contribution = False
    await poller.poll_once()
    assert "qbit-completion:new" in store.completed
    assert live.calls == [
        ("fetch", "new"),
        ("contribute", "new"),
        ("contribute", "new"),
    ]
    assert store.counters == {
        "fetched": 1,
        "matches": 1,
        "placed": 1,
        "uploaded": 1,
    }
    assert [event_type for event_type, _, _ in store.activities] == [
        "qbit_fetch",
        "qbit_contribute",
        "qbit_contribute",
    ]
    assert [details["status"] for _, _, details in store.activities] == [
        "success",
        "warning",
        "success",
    ]
    poller.start()
    poller.start()
    await asyncio.sleep(0)
    await poller.close()

    adapter = QBitLiveWorkflow(
        SABLiveWorkflow(
            settings=live_settings(Path("/tmp"), dry_run=True),
            path_mapper=FixedPathMapper(Path("/tmp")),
            crowdnfo=RecordingDownloader(),
            contribution=RecordingContribution(),
        )
    )
    event = adapter._event(completed_torrent("adapted"))  # noqa: SLF001
    assert event.nzo_id == "adapted" and event.storage_path == "/data/adapted"


@pytest.mark.asyncio
async def test_qbit_completion_poller_does_not_count_noop_live_actions() -> None:
    store = CompletionMemory()
    poller = QBitCompletedPoller(
        qbit=PollQBit([completed_torrent("noop")]),
        live_service=PollLiveService(
            fetch_performed=False,
            contribution_performed=False,
        ),
        store=store,
        fetch_enabled=True,
        contribute_enabled=True,
        poll_interval=0.01,
    )

    result = await poller.poll_once()

    assert result["noop"].errors == {}
    assert store.counters == {}
    assert [details["status"] for _, _, details in store.activities] == [
        "info",
        "info",
    ]
    assert {
        "qbit-completion:noop",
        "qbit-completion:noop:fetch",
        "qbit-completion:noop:contribute",
    }.issubset(store.completed)
    await poller.close()


class QueueStore:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, object]] = {}
        self.count = 0

    async def start_job(self, *, action: str, target: str | None = None) -> str:
        self.count += 1
        job_id = f"job-{self.count}"
        self.jobs[job_id] = {"action": action, "target": target, "status": "running"}
        return job_id

    async def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        detail: str | None = None,
    ) -> None:
        self.jobs[job_id].update(status=status, detail=detail)


class FailingQueuedRuntime:
    async def run_queued_action(self, **kwargs: object) -> None:
        del kwargs
        raise RuntimeError("token=must-not-leak")


@pytest.mark.asyncio
async def test_action_queue_validates_binding_failure_and_shutdown() -> None:
    store = QueueStore()
    with pytest.raises(ValueError, match="concurrency"):
        InProcessActionQueue(store=store, max_concurrency=0)
    with pytest.raises(ValueError, match="pending"):
        InProcessActionQueue(store=store, max_pending=0)

    queue = InProcessActionQueue(store=store, max_concurrency=1)
    with pytest.raises(RuntimeError, match="not bound"):
        await queue.enqueue(action="scan_and_repair", payload={})
    runtime = FailingQueuedRuntime()
    queue.bind(runtime)  # type: ignore[arg-type]
    queue.bind(runtime)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="already bound"):
        queue.bind(FailingQueuedRuntime())  # type: ignore[arg-type]
    job_id = await queue.enqueue(
        action="repair_torrent",
        payload={"torrent_hash": "deadbeef"},
    )
    await asyncio.wait_for(queue._pending.join(), timeout=0.2)  # noqa: SLF001
    assert store.jobs[job_id]["status"] == "failed"
    assert store.jobs[job_id]["detail"] == "operation failed"
    await queue.close()
    with pytest.raises(RuntimeError, match="closed"):
        await queue.enqueue(action="scan_and_repair", payload={})
