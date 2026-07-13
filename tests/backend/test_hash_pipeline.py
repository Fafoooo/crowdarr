from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import Counter
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import pytest

import backend.runtime as runtime_module
from backend.connectors.qbit import MissingNFO, TorrentFile, TorrentSnapshot
from backend.connectors.sab import SABCompletionEvent
from backend.core.contribution import ContributionItem
from backend.core.files import PathMapper, PathMapping
from backend.core.hashing import HashResult
from backend.core.library import LibraryMediaItem
from backend.core.repair import RepairStatus, TorrentRepairService
from backend.core.settings import (
    AppSettings,
    ConnectorSettings,
    ContributionSettings,
    DownloadMode,
    PathMappingSetting,
)
from backend.db.operations import OperationsStore
from backend.runtime import CrowdarrrRuntime

DIGEST = hashlib.sha256(b"media-payload").hexdigest()


class MemoryHashCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, int, int], str] = {}

    async def get_file_hash(
        self,
        *,
        path: Path,
        size: int,
        mtime_ns: int,
    ) -> str | None:
        return self.values.get((str(path), size, mtime_ns))

    async def put_file_hash(
        self,
        *,
        path: Path,
        size: int,
        mtime_ns: int,
        sha256: str,
    ) -> None:
        self.values[(str(path), size, mtime_ns)] = sha256


class RecordingHashService:
    def __init__(self, digest: str = DIGEST) -> None:
        self.digest = digest
        self.paths: list[Path] = []

    async def hash_file(self, path: Path) -> HashResult:
        self.paths.append(Path(path))
        return HashResult(
            digest=self.digest,
            bytes_hashed=0,
            cache_hit=False,
        )


@pytest.mark.asyncio
async def test_file_hash_cache_persists_exact_path_size_mtime_key(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite3"
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"media-payload")
    stat = media.stat()
    first = OperationsStore(database)
    await first.initialize()

    await first.put_file_hash(
        path=media,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=DIGEST,
    )
    await first.close()

    reopened = OperationsStore(database)
    await reopened.initialize()
    cached = await reopened.get_file_hash(
        path=media,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
    changed_mtime = await reopened.get_file_hash(
        path=media,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns + 1,
    )
    changed_size = await reopened.get_file_hash(
        path=media,
        size=stat.st_size + 1,
        mtime_ns=stat.st_mtime_ns,
    )
    await reopened.close()

    assert cached == DIGEST
    assert changed_mtime is None
    assert changed_size is None


@pytest.mark.asyncio
async def test_async_hash_service_uses_to_thread_and_bounds_parallel_hashes(
    tmp_path: Path,
) -> None:
    hash_service_type = getattr(runtime_module, "AsyncHashService", None)
    assert hash_service_type is not None, "backend.runtime must expose AsyncHashService"

    media_paths = [tmp_path / f"Movie-{index}.mkv" for index in range(3)]
    for media_path in media_paths:
        media_path.write_bytes(b"media-payload")

    guard = threading.Lock()
    release = threading.Event()
    two_started = threading.Event()
    main_thread = threading.get_ident()
    active = 0
    maximum_active = 0
    started_count = 0
    worker_threads: set[int] = set()

    def blocking_hash(
        path: Path,
        *,
        max_size: int,
        cache: object | None = None,
    ) -> HashResult:
        nonlocal active, maximum_active, started_count
        del max_size, cache
        with guard:
            active += 1
            started_count += 1
            maximum_active = max(maximum_active, active)
            worker_threads.add(threading.get_ident())
            if started_count == 2:
                two_started.set()
        assert release.wait(timeout=1.0)
        with guard:
            active -= 1
        return HashResult(
            digest=hashlib.sha256(path.name.encode()).hexdigest(),
            bytes_hashed=path.stat().st_size,
            cache_hit=False,
        )

    service = hash_service_type(
        cache=MemoryHashCache(),
        max_size_bytes=1024,
        max_concurrency=2,
        hash_function=blocking_hash,
    )
    tasks = [asyncio.create_task(service.hash_file(path)) for path in media_paths]
    try:
        started = await asyncio.to_thread(two_started.wait, 0.3)
        assert started is True
        with guard:
            assert started_count == 2
            assert maximum_active == 2
            assert main_thread not in worker_threads
    finally:
        release.set()

    results = await asyncio.gather(*tasks)
    assert all(result.digest is not None for result in results)
    assert maximum_active == 2


class RecordingCrowdNFO:
    def __init__(self, payload: bytes = b"raw-nfo\r\n\xff") -> None:
        self.payload = payload
        self.downloads: list[tuple[str, str | None]] = []

    async def download_nfo(
        self,
        *,
        release_name: str,
        media_sha256: str | None = None,
    ) -> bytes:
        self.downloads.append((release_name, media_sha256))
        return self.payload


class RepairQBit:
    def __init__(self, snapshot: TorrentSnapshot) -> None:
        self.snapshot = snapshot

    async def get_torrent(self, torrent_hash: str) -> TorrentSnapshot:
        assert torrent_hash == self.snapshot.torrent_hash
        return self.snapshot

    async def set_file_priority(
        self,
        torrent_hash: str,
        file_ids: list[int],
        priority: int,
    ) -> None:
        assert torrent_hash == self.snapshot.torrent_hash
        assert file_ids == [1]
        assert priority == 1

    async def force_recheck(self, torrent_hash: str) -> None:
        assert torrent_hash == self.snapshot.torrent_hash

    async def resume(self, torrent_hash: str) -> TorrentSnapshot:
        assert torrent_hash == self.snapshot.torrent_hash
        return self.snapshot


@pytest.mark.asyncio
async def test_qbit_repair_hashes_video_and_prefers_hash_lookup(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    release_root = data_root / "release"
    release_root.mkdir(parents=True)
    media = release_root / "Movie.mkv"
    media.write_bytes(b"media-payload")
    snapshot = TorrentSnapshot(
        torrent_hash="torrent-hash",
        name="Movie.2026-GROUP",
        category="movies",
        content_path="/data/release",
        progress=1.0,
        state="uploading",
        files=[
            TorrentFile(0, "Movie.mkv", media.stat().st_size, 1.0, 1),
            TorrentFile(1, "Movie.nfo", 20, 1.0, 1),
        ],
        local_content_path=release_root,
    )
    crowdnfo = RecordingCrowdNFO()
    hasher = RecordingHashService()
    mapper = PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=data_root)],
        allowed_roots=[data_root],
    )
    service = TorrentRepairService(
        crowdnfo=crowdnfo,
        qbit=RepairQBit(snapshot),
        path_mapper=mapper,
        allowed_roots=[data_root],
        hash_service=hasher,
        poll_interval=0.01,
        recheck_timeout=0.1,
    )
    candidate = MissingNFO(
        torrent_hash=snapshot.torrent_hash,
        torrent_name=snapshot.name,
        file_index=1,
        relative_path=PurePosixPath("Movie.nfo"),
        reported_path=PurePosixPath("/data/release/Movie.nfo"),
    )

    result = await service.repair(candidate)

    assert result.status is RepairStatus.SUCCESS
    assert hasher.paths == [media]
    assert crowdnfo.downloads == [(snapshot.name, DIGEST)]


@pytest.mark.asyncio
async def test_qbit_repair_never_hashes_an_incomplete_video(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    release_root = data_root / "release"
    release_root.mkdir(parents=True)
    media = release_root / "Movie.mkv"
    media.write_bytes(b"incomplete-media-payload")
    snapshot = TorrentSnapshot(
        torrent_hash="torrent-hash",
        name="Movie.2026-GROUP",
        category="movies",
        content_path="/data/release",
        progress=0.999,
        state="stalledDL",
        files=[
            TorrentFile(0, "Movie.mkv", media.stat().st_size, 0.9999, 1),
            TorrentFile(1, "Movie.nfo", 20, 0.0, 1),
        ],
        local_content_path=release_root,
    )
    crowdnfo = RecordingCrowdNFO()
    hasher = RecordingHashService()
    mapper = PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=data_root)],
        allowed_roots=[data_root],
    )
    service = TorrentRepairService(
        crowdnfo=crowdnfo,
        qbit=RepairQBit(snapshot),
        path_mapper=mapper,
        allowed_roots=[data_root],
        hash_service=hasher,
        auto_recheck=False,
    )
    candidate = MissingNFO(
        torrent_hash=snapshot.torrent_hash,
        torrent_name=snapshot.name,
        file_index=1,
        relative_path=PurePosixPath("Movie.nfo"),
        reported_path=PurePosixPath("/data/release/Movie.nfo"),
    )

    result = await service.repair(candidate)

    assert result.status is RepairStatus.PLACED_UNVERIFIED
    assert hasher.paths == []
    assert crowdnfo.downloads == [(snapshot.name, None)]


class RuntimeStore:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.jobs: dict[str, str] = {}

    async def start_job(self, *, action: str, target: str | None = None) -> str:
        del target
        job_id = f"{action}-{len(self.jobs) + 1}"
        self.jobs[job_id] = "running"
        return job_id

    async def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        detail: str | None = None,
    ) -> None:
        del detail
        self.jobs[job_id] = status

    async def increment_counter(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    async def record_activity(self, **kwargs: object) -> None:
        del kwargs

    async def record_miss(self, **kwargs: object) -> str:
        del kwargs
        return "miss-1"

    async def get_counters(self) -> Mapping[str, int]:
        return dict(self.counters)

    async def recent_activity(self, *, limit: int) -> list[dict[str, object]]:
        del limit
        return []


class LibraryConnector:
    def __init__(self, item: LibraryMediaItem) -> None:
        self.item = item

    async def scan(self) -> list[LibraryMediaItem]:
        return [self.item]


@pytest.mark.asyncio
async def test_library_backfill_passes_media_hash_to_crowdnfo(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media = data_root / "movies" / "Movie" / "Movie.mkv"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"media-payload")
    item = LibraryMediaItem("Movie.2026-GROUP", media, source="radarr")
    crowdnfo = RecordingCrowdNFO()
    hasher = RecordingHashService()
    runtime = CrowdarrrRuntime(
        settings=AppSettings(
            download_mode=DownloadMode.NEW_AND_BACKFILL,
            dry_run=False,
            radarr=ConnectorSettings(
                enabled=True,
                base_url="http://radarr:7878",
            ),
            path_mappings=[
                PathMappingSetting(remote_root="/data", local_root=str(data_root))
            ],
        ),
        store=RuntimeStore(),
        crowdnfo=crowdnfo,
        library_connectors={"radarr": LibraryConnector(item)},
        hash_service=hasher,
    )

    outcome = await runtime.scan_libraries()

    assert outcome.status == "success"
    assert hasher.paths == [media]
    assert crowdnfo.downloads == [(item.release_name, DIGEST)]


class RecordingContribution:
    def __init__(self) -> None:
        self.items: list[ContributionItem] = []

    async def contribute(
        self,
        item: ContributionItem,
        *,
        include_nfo: bool,
        include_mediainfo: bool,
        include_filelist: bool,
    ) -> object:
        assert include_nfo and include_mediainfo and include_filelist
        self.items.append(item)
        return object()


@pytest.mark.asyncio
async def test_sab_live_pipeline_uses_hash_for_fetch_and_contribution(
    tmp_path: Path,
) -> None:
    pipeline_type = getattr(runtime_module, "SABLiveWorkflow", None)
    assert pipeline_type is not None, "backend.runtime must expose SABLiveWorkflow"

    data_root = tmp_path / "data"
    release_root = data_root / "downloads" / "Movie"
    release_root.mkdir(parents=True)
    media = release_root / "Movie.mkv"
    media.write_bytes(b"media-payload")
    settings = AppSettings(
        download_mode=DownloadMode.NEW_ONLY,
        dry_run=False,
        contribute=ContributionSettings(enabled=True),
        path_mappings=[
            PathMappingSetting(remote_root="/data", local_root=str(data_root))
        ],
    )
    mapper = PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=data_root)],
        allowed_roots=[data_root],
    )
    crowdnfo = RecordingCrowdNFO()
    contribution = RecordingContribution()
    hasher = RecordingHashService()
    pipeline = pipeline_type(
        settings=settings,
        path_mapper=mapper,
        crowdnfo=crowdnfo,
        contribution=contribution,
        hash_service=hasher,
    )
    event = SABCompletionEvent(
        release_name="Movie.2026-GROUP",
        storage_path="/data/downloads/Movie",
        category="movies",
        nzo_id="SABnzbd_nzo_1",
    )

    await pipeline.fetch_missing(event)
    await pipeline.contribute(event)

    assert crowdnfo.downloads == [(event.release_name, DIGEST)]
    assert len(contribution.items) == 1
    assert contribution.items[0].media_sha256 == DIGEST
    assert set(hasher.paths) == {media}


class CompletedQBit:
    def __init__(self, torrents: list[TorrentSnapshot]) -> None:
        self.torrents = torrents
        self.calls = 0

    async def list_torrents(self) -> list[TorrentSnapshot]:
        self.calls += 1
        return self.torrents


class QBitLiveService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fetch_missing(self, torrent: TorrentSnapshot) -> None:
        self.calls.append(("fetch", torrent.torrent_hash))

    async def contribute(self, torrent: TorrentSnapshot) -> None:
        self.calls.append(("contribute", torrent.torrent_hash))


class IdempotencyStore:
    def __init__(self) -> None:
        self.completed: set[str] = set()

    async def was_completed(self, key: str) -> bool:
        return key in self.completed

    async def mark_completed(self, key: str) -> None:
        self.completed.add(key)


@pytest.mark.asyncio
async def test_qbit_completed_poller_runs_new_only_and_live_out_once() -> None:
    poller_type = getattr(runtime_module, "QBitCompletedPoller", None)
    assert poller_type is not None, "backend.runtime must expose QBitCompletedPoller"

    completed = TorrentSnapshot(
        torrent_hash="complete-hash",
        name="Movie.2026-GROUP",
        category="movies",
        content_path="/data/downloads/Movie",
        progress=1.0,
        state="uploading",
    )
    incomplete = TorrentSnapshot(
        torrent_hash="incomplete-hash",
        name="Other.2026-GROUP",
        category="movies",
        content_path="/data/downloads/Other",
        progress=0.99,
        state="downloading",
    )
    qbit = CompletedQBit([completed, incomplete])
    live = QBitLiveService()
    store = IdempotencyStore()
    poller = poller_type(
        qbit=qbit,
        live_service=live,
        store=store,
        fetch_enabled=True,
        contribute_enabled=True,
    )

    await poller.poll_once()
    await poller.poll_once()

    assert live.calls == [
        ("fetch", completed.torrent_hash),
        ("contribute", completed.torrent_hash),
    ]
    assert qbit.calls == 2
    assert any(completed.torrent_hash in key for key in store.completed)
    assert all(incomplete.torrent_hash not in key for key in store.completed)
