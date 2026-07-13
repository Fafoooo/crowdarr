from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.connectors.health import ConnectorHealth
from backend.connectors.qbit import MissingNFO, TorrentFile, TorrentSnapshot
from backend.connectors.sab import SABCompletionEvent, SABWebhookHandler
from backend.core.files import PathMapper, PathMapping
from backend.core.library import LibraryMediaItem
from backend.core.repair import RepairResult, RepairStatus, TorrentRepairService
from backend.core.settings import (
    AppSettings,
    ConnectorSettings,
    ContributionSettings,
    DownloadMode,
    PathMappingSetting,
)
from backend.runtime import CrowdarrrRuntime

RAW_NFO = b"\xffbyte-exact\r\nrelease nfo\r\n"


def http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://crowdnfo.test/api/releases/test/files/best")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"CrowdNFO returned {status_code}",
        request=request,
        response=response,
    )


def connector_settings(name: str, *, enabled: bool) -> ConnectorSettings:
    return ConnectorSettings(enabled=enabled, base_url=f"http://{name}.test:8080")


def runtime_settings(
    tmp_path: Path,
    *,
    mode: DownloadMode = DownloadMode.NEW_AND_BACKFILL,
    dry_run: bool = False,
    qbit_enabled: bool = True,
    sab_enabled: bool = False,
    radarr_enabled: bool = False,
    sonarr_enabled: bool = False,
    contribute: bool = False,
) -> AppSettings:
    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    return AppSettings(
        qbittorrent=connector_settings("qbittorrent", enabled=qbit_enabled),
        sabnzbd=connector_settings("sabnzbd", enabled=sab_enabled),
        radarr=connector_settings("radarr", enabled=radarr_enabled),
        sonarr=connector_settings("sonarr", enabled=sonarr_enabled),
        download_mode=mode,
        dry_run=dry_run,
        contribute=ContributionSettings(enabled=contribute),
        path_mappings=[
            PathMappingSetting(remote_root="/data", local_root=str(data_root))
        ],
    )


def torrent_files(release: str, *, nfo_progress: float = 0.0) -> list[TorrentFile]:
    return [
        TorrentFile(
            index=7,
            path=f"{release}/{release}.nfo",
            size=8_192,
            progress=nfo_progress,
            priority=0,
        ),
        TorrentFile(
            index=8,
            path=f"{release}/{release}.mkv",
            size=8_000_000_000,
            progress=1.0,
            priority=1,
        ),
    ]


def torrent_snapshot(
    torrent_hash: str,
    release: str,
    *,
    progress: float = 0.999,
    state: str = "stalledDL",
    files: list[TorrentFile] | None = None,
) -> TorrentSnapshot:
    return TorrentSnapshot(
        torrent_hash=torrent_hash,
        name=release,
        category="cross-seed-link",
        content_path="/data/cross-seeds",
        progress=progress,
        state=state,
        files=files or [],
    )


class FakeRuntimeStore:
    """Small persistence fake defining the runtime repository boundary."""

    def __init__(self) -> None:
        self.counters = {
            "fetched": 0,
            "matches": 0,
            "misses": 0,
            "placed": 0,
            "repaired": 0,
            "uploaded": 0,
        }
        self.activities: list[dict[str, Any]] = []
        self.misses: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.negative_lookups: dict[str, dict[str, Any]] = {}
        self.repair_states: dict[str, dict[str, Any]] = {}
        self.repaired_once: set[str] = set()

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
        result: object | None = None,
    ) -> None:
        self.jobs[job_id].update(status=status, detail=detail, result=result)

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
    ) -> str:
        activity_id = f"activity-{len(self.activities) + 1}"
        self.activities.append(
            {
                "id": activity_id,
                "type": activity_type,
                "status": status,
                "title": title,
                "message": message,
                "miss_id": miss_id,
                "created_at": f"2026-01-01T00:00:{len(self.activities):02d}Z",
            }
        )
        return activity_id

    async def record_miss(
        self,
        *,
        source: str,
        release_name: str,
        reason: str,
        retryable: bool,
    ) -> str:
        miss_id = f"miss-{len(self.misses) + 1}"
        self.misses.append(
            {
                "id": miss_id,
                "source": source,
                "release_name": release_name,
                "reason": reason,
                "retryable": retryable,
            }
        )
        return miss_id

    async def get_counters(self) -> dict[str, int]:
        return dict(self.counters)

    async def recent_activity(self, *, limit: int) -> list[dict[str, Any]]:
        return list(reversed(self.activities[-limit:]))

    async def get_negative_lookup(self, release_name: str) -> dict[str, Any] | None:
        return self.negative_lookups.get(release_name.casefold())

    async def cache_negative_lookup(
        self,
        *,
        release_name: str,
        reason: str,
        ttl_seconds: int,
    ) -> None:
        self.negative_lookups[release_name.casefold()] = {
            "release_name": release_name,
            "reason": reason,
            "ttl_seconds": ttl_seconds,
        }

    async def put_repair_state(
        self,
        *,
        torrent_hash: str,
        release_name: str,
        outcome: str,
        message: str,
        retryable: bool,
    ) -> None:
        self.repair_states[torrent_hash] = {
            "release_name": release_name,
            "outcome": outcome,
            "message": message,
            "retryable": retryable,
        }

    async def list_repair_states(self) -> dict[str, dict[str, Any]]:
        return dict(self.repair_states)

    async def list_repair_targets(self) -> set[str]:
        return set()

    async def record_repaired_once(
        self,
        *,
        torrent_hash: str,
        release_name: str,
        message: str,
    ) -> bool:
        if torrent_hash in self.repaired_once:
            return False
        self.repaired_once.add(torrent_hash)
        self.counters["repaired"] += 1
        await self.put_repair_state(
            torrent_hash=torrent_hash,
            release_name=release_name,
            outcome="fixed",
            message=message,
            retryable=False,
        )
        return True


class FakeQBit:
    def __init__(
        self,
        snapshots: list[TorrentSnapshot],
        files: dict[str, list[TorrentFile] | Exception],
        *,
        list_error: Exception | None = None,
        health: ConnectorHealth | None = None,
    ) -> None:
        self.snapshots = {snapshot.torrent_hash: snapshot for snapshot in snapshots}
        self.files = files
        self.list_error = list_error
        self.health = health or ConnectorHealth(True, version="5.0.4")
        self.calls: list[tuple[Any, ...]] = []
        self.rechecked: set[str] = set()

    async def list_torrents(self) -> list[TorrentSnapshot]:
        self.calls.append(("list_torrents",))
        if self.list_error is not None:
            raise self.list_error
        return list(self.snapshots.values())

    async def list_files(self, torrent_hash: str) -> list[TorrentFile]:
        self.calls.append(("list_files", torrent_hash))
        result = self.files[torrent_hash]
        if isinstance(result, Exception):
            raise result
        return result

    async def get_torrent(self, torrent_hash: str) -> TorrentSnapshot:
        self.calls.append(("get_torrent", torrent_hash))
        snapshot = self.snapshots[torrent_hash]
        files = self.files[torrent_hash]
        if isinstance(files, Exception):
            raise files
        if torrent_hash not in self.rechecked:
            return TorrentSnapshot(
                torrent_hash=snapshot.torrent_hash,
                name=snapshot.name,
                category=snapshot.category,
                content_path=snapshot.content_path,
                progress=snapshot.progress,
                state=snapshot.state,
                files=files,
            )
        return torrent_snapshot(
            torrent_hash,
            snapshot.name,
            progress=1.0,
            state="uploading",
            files=torrent_files(snapshot.name, nfo_progress=1.0),
        )

    async def set_file_priority(
        self, torrent_hash: str, file_ids: list[int], priority: int
    ) -> None:
        self.calls.append(("priority", torrent_hash, file_ids, priority))

    async def force_recheck(self, torrent_hash: str) -> None:
        self.calls.append(("recheck", torrent_hash))
        self.rechecked.add(torrent_hash)

    async def resume(self, torrent_hash: str) -> TorrentSnapshot:
        self.calls.append(("resume", torrent_hash))
        return await self.get_torrent(torrent_hash)

    async def healthcheck(self) -> ConnectorHealth:
        self.calls.append(("healthcheck",))
        return self.health


class FakeCrowdNFO:
    def __init__(
        self,
        responses: dict[str, bytes | Exception],
        *,
        health: ConnectorHealth | None = None,
    ) -> None:
        self.responses = responses
        self.health = health or ConnectorHealth(True, version="beta")
        self.download_calls: list[tuple[str, str | None]] = []

    async def download_nfo(
        self,
        *,
        release_name: str,
        media_sha256: str | None = None,
    ) -> bytes:
        self.download_calls.append((release_name, media_sha256))
        result = self.responses[release_name]
        if isinstance(result, Exception):
            raise result
        return result

    async def healthcheck(self) -> ConnectorHealth:
        return self.health


class RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, bytes, bool]] = []

    def __call__(self, path: Path, payload: bytes, *, overwrite: bool) -> None:
        self.calls.append((path, payload, overwrite))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


class FakeRepairService:
    def __init__(self, results: dict[str, RepairResult | Exception]) -> None:
        self.results = results
        self.calls: list[MissingNFO] = []

    async def repair(self, candidate: MissingNFO) -> RepairResult:
        self.calls.append(candidate)
        result = self.results[candidate.torrent_hash]
        if isinstance(result, Exception):
            raise result
        return result


class BatchRepairService:
    def __init__(self, target_root: Path) -> None:
        self.target_root = target_root
        self.batches: list[list[MissingNFO]] = []

    async def repair(self, candidate: MissingNFO) -> RepairResult:
        raise AssertionError(f"runtime repaired candidate individually: {candidate}")

    async def repair_many(self, candidates: list[MissingNFO]) -> list[RepairResult]:
        self.batches.append(list(candidates))
        return [
            RepairResult(
                status=RepairStatus.SUCCESS,
                target_path=self.target_root / candidate.relative_path.name,
                verified=True,
                seeding=True,
            )
            for candidate in candidates
        ]


class FakeLibraryConnector:
    def __init__(
        self,
        items: list[LibraryMediaItem] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.items = items or []
        self.error = error
        self.calls = 0

    async def scan(self) -> list[LibraryMediaItem]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.items


class FakeSABLiveService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, SABCompletionEvent]] = []

    async def fetch_missing(self, event: SABCompletionEvent) -> None:
        self.calls.append(("fetch", event))
        raise ConnectionError("live-in unavailable; api_key=must-not-leak")

    async def contribute(self, event: SABCompletionEvent) -> None:
        self.calls.append(("contribute", event))


class FakeHealthConnector:
    def __init__(self, result: ConnectorHealth | Exception) -> None:
        self.result = result

    async def healthcheck(self) -> ConnectorHealth:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeActionQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def enqueue(self, *, action: str, payload: dict[str, str]) -> str:
        self.calls.append((action, payload))
        return f"queued-{len(self.calls)}"


@pytest.mark.asyncio
async def test_discovery_hydrates_qbit_files_and_exposes_only_stuck_candidates(
    tmp_path: Path,
) -> None:
    stuck = torrent_snapshot("stuck", "Release.Stuck-GROUP")
    complete = torrent_snapshot(
        "complete",
        "Release.Complete-GROUP",
        progress=1.0,
        state="uploading",
    )
    qbit = FakeQBit(
        [stuck, complete],
        {
            "stuck": torrent_files(stuck.name),
            "complete": torrent_files(complete.name, nfo_progress=1.0),
        },
    )
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=FakeRuntimeStore(),
        qbit=qbit,
    )

    candidates = await runtime.discover_stuck_torrents()

    assert len(candidates) == 1
    assert isinstance(candidates[0], MissingNFO)
    assert candidates[0].torrent_hash == "stuck"
    assert candidates[0].relative_path.as_posix().endswith(".nfo")
    assert qbit.calls == [("list_torrents",), ("list_files", "stuck")]


@pytest.mark.asyncio
async def test_discovery_skips_one_unavailable_file_list_and_keeps_scanning(
    tmp_path: Path,
) -> None:
    unavailable = torrent_snapshot("unavailable", "Release.Unavailable-GROUP")
    stuck = torrent_snapshot("stuck", "Release.Stuck-GROUP")
    qbit = FakeQBit(
        [unavailable, stuck],
        {
            unavailable.torrent_hash: ConnectionError("torrent disappeared"),
            stuck.torrent_hash: torrent_files(stuck.name),
        },
    )
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=FakeRuntimeStore(),
        qbit=qbit,
    )

    candidates = await runtime.discover_stuck_torrents()

    assert [candidate.torrent_hash for candidate in candidates] == ["stuck"]
    assert qbit.calls == [
        ("list_torrents",),
        ("list_files", "unavailable"),
        ("list_files", "stuck"),
    ]


@pytest.mark.asyncio
async def test_scan_and_repair_preserves_bytes_and_persists_job_counters_and_miss(
    tmp_path: Path,
) -> None:
    good = torrent_snapshot("good", "Release.Good-GROUP")
    missing = torrent_snapshot("missing", "Release.Missing-GROUP")
    qbit = FakeQBit(
        [good, missing],
        {
            good.torrent_hash: torrent_files(good.name),
            missing.torrent_hash: torrent_files(missing.name),
        },
    )
    crowdnfo = FakeCrowdNFO(
        {
            good.name: RAW_NFO,
            missing.name: http_error(404),
        }
    )
    data_root = tmp_path / "data"
    mapper = PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=data_root)],
        allowed_roots=[data_root],
    )
    writer = RecordingWriter()
    repair = TorrentRepairService(
        crowdnfo=crowdnfo,
        qbit=qbit,
        path_mapper=mapper,
        atomic_writer=writer,
        poll_interval=0.01,
        recheck_timeout=1.0,
    )
    store = FakeRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=store,
        qbit=qbit,
        repair_service=repair,
    )

    outcome = await runtime.scan_and_repair()

    target = data_root / "cross-seeds/Release.Good-GROUP/Release.Good-GROUP.nfo"
    assert target.read_bytes() == RAW_NFO
    assert writer.calls == [(target, RAW_NFO, True)]
    assert outcome.job_id == "job-1"
    assert outcome.status == "partial"
    assert store.counters == {
        "fetched": 1,
        "matches": 1,
        "misses": 1,
        "placed": 1,
        "repaired": 1,
        "uploaded": 0,
    }
    assert store.jobs["job-1"]["status"] == "partial"
    assert store.misses == [
        {
            "id": "miss-1",
            "source": "qbittorrent",
            "release_name": missing.name,
            "reason": "not found",
            "retryable": False,
        }
    ]
    assert {activity["type"] for activity in store.activities} >= {"repair", "miss"}
    miss_activity = next(
        activity for activity in store.activities if activity["type"] == "miss"
    )
    assert missing.name in miss_activity["message"]
    assert "not found" in miss_activity["message"]


@pytest.mark.asyncio
async def test_per_torrent_repair_targets_only_the_requested_hash(
    tmp_path: Path,
) -> None:
    first = torrent_snapshot("first", "Release.First-GROUP")
    second = torrent_snapshot("second", "Release.Second-GROUP")
    qbit = FakeQBit(
        [first, second],
        {
            first.torrent_hash: torrent_files(first.name),
            second.torrent_hash: torrent_files(second.name),
        },
    )
    repair = FakeRepairService(
        {
            "second": RepairResult(
                status=RepairStatus.SUCCESS,
                target_path=tmp_path / "second.nfo",
                verified=True,
                seeding=True,
            )
        }
    )
    store = FakeRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=store,
        qbit=qbit,
        repair_service=repair,
    )

    outcome = await runtime.repair_torrent("second")

    assert outcome.status == "success"
    assert outcome.job_id == "job-1"
    assert [candidate.torrent_hash for candidate in repair.calls] == ["second"]
    assert ("list_torrents",) not in qbit.calls
    assert qbit.calls[:1] == [("get_torrent", "second")]
    assert store.counters["repaired"] == 1
    assert outcome.result == {
        "message": "NFO verified; torrent is seeding",
        "outcome": "fixed",
        "release_name": second.name,
        "retryable": False,
        "torrent_hash": second.torrent_hash,
    }


@pytest.mark.asyncio
async def test_definitive_miss_is_cached_once_and_exposed_on_dashboard(
    tmp_path: Path,
) -> None:
    missing = torrent_snapshot("missing", "Release.NotAvailable-GROUP")
    repair = FakeRepairService({missing.torrent_hash: http_error(404)})
    store = FakeRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=store,
        qbit=FakeQBit(
            [missing],
            {missing.torrent_hash: torrent_files(missing.name)},
        ),
        repair_service=repair,
        negative_cache_ttl_seconds=43_200,
    )

    first = await runtime.repair_torrent(missing.torrent_hash)
    second = await runtime.repair_torrent(missing.torrent_hash)
    dashboard = await runtime.dashboard_snapshot()

    assert first.status == "skipped"
    assert second.status == "skipped"
    assert len(repair.calls) == 1
    assert store.counters["misses"] == 1
    assert first.result == {
        "message": "Release.NotAvailable-GROUP — not found in CrowdNFO",
        "outcome": "not_available",
        "release_name": missing.name,
        "retryable": False,
        "torrent_hash": missing.torrent_hash,
    }
    assert second.result == first.result
    row = dashboard.stuck_torrents[0]
    assert row.repair_outcome == "not_available"
    assert row.repair_message == first.result["message"]
    assert row.retryable is False


@pytest.mark.asyncio
async def test_transient_fetch_failure_is_retryable_and_not_counted_as_a_miss(
    tmp_path: Path,
) -> None:
    torrent = torrent_snapshot("transient", "Release.Transient-GROUP")
    repair = FakeRepairService(
        {
            torrent.torrent_hash: httpx.ConnectError(
                "temporary connection failure",
                request=httpx.Request("GET", "https://crowdnfo.test/api/releases"),
            )
        }
    )
    store = FakeRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=store,
        qbit=FakeQBit(
            [torrent],
            {torrent.torrent_hash: torrent_files(torrent.name)},
        ),
        repair_service=repair,
    )

    outcome = await runtime.repair_torrent(torrent.torrent_hash)

    assert outcome.status == "failed"
    assert store.counters["misses"] == 0
    assert store.repair_states[torrent.torrent_hash]["outcome"] == "fetch_failed"
    assert outcome.result == {
        "message": "Release.Transient-GROUP — connection failed; retry available",
        "outcome": "fetch_failed",
        "release_name": torrent.name,
        "retryable": True,
        "torrent_hash": torrent.torrent_hash,
    }
    assert torrent.name in store.activities[-1]["message"]


@pytest.mark.asyncio
async def test_dashboard_reconciles_a_delayed_completed_recheck_once(
    tmp_path: Path,
) -> None:
    completed = torrent_snapshot(
        "delayed",
        "Release.Delayed-GROUP",
        progress=1.0,
        state="uploading",
        files=torrent_files("Release.Delayed-GROUP", nfo_progress=1.0),
    )
    store = FakeRuntimeStore()
    await store.put_repair_state(
        torrent_hash=completed.torrent_hash,
        release_name=completed.name,
        outcome="verification_pending",
        message="recheck still running",
        retryable=True,
    )
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=store,
        qbit=FakeQBit(
            [completed],
            {completed.torrent_hash: completed.files},
        ),
    )

    first = await runtime.dashboard_snapshot()
    second = await runtime.dashboard_snapshot()

    assert first.counters.repaired == 1
    assert second.counters.repaired == 1
    repaired = [
        activity
        for activity in store.activities
        if activity["title"] == "Torrent repaired after delayed recheck"
    ]
    assert len(repaired) == 1
    assert completed.name in repaired[0]["message"]


@pytest.mark.asyncio
async def test_runtime_batches_missing_nfos_and_counts_one_repaired_torrent(
    tmp_path: Path,
) -> None:
    snapshot = torrent_snapshot("batch", "Release.Batch-GROUP")
    files = torrent_files(snapshot.name)
    files.insert(
        1,
        TorrentFile(
            index=9,
            path=f"{snapshot.name}/{snapshot.name}.proof.nfo",
            size=4_096,
            progress=0.0,
            priority=0,
        ),
    )
    repair = BatchRepairService(tmp_path)
    store = FakeRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=store,
        qbit=FakeQBit([snapshot], {snapshot.torrent_hash: files}),
        repair_service=repair,
    )

    outcome = await runtime.scan_and_repair()

    assert outcome.status == "success"
    assert len(repair.batches) == 1
    assert [candidate.file_index for candidate in repair.batches[0]] == [7, 9]
    assert store.counters["fetched"] == 2
    assert store.counters["matches"] == 2
    assert store.counters["repaired"] == 1


@pytest.mark.asyncio
async def test_downloaded_nfos_count_as_fetches_and_matches_before_verification(
    tmp_path: Path,
) -> None:
    timeout = torrent_snapshot("timeout", "Release.Timeout-GROUP")
    mismatch = torrent_snapshot("mismatch", "Release.Mismatch-GROUP")
    repair = FakeRepairService(
        {
            timeout.torrent_hash: RepairResult(
                status=RepairStatus.TIMEOUT,
                target_path=tmp_path / "timeout.nfo",
                retryable=True,
            ),
            mismatch.torrent_hash: RepairResult(
                status=RepairStatus.MISMATCH,
                target_path=tmp_path / "mismatch.nfo",
                retryable=True,
            ),
        }
    )
    store = FakeRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=store,
        qbit=FakeQBit(
            [timeout, mismatch],
            {
                timeout.torrent_hash: torrent_files(timeout.name),
                mismatch.torrent_hash: torrent_files(mismatch.name),
            },
        ),
        repair_service=repair,
    )

    outcome = await runtime.scan_and_repair()

    assert outcome.status == "failed"
    assert store.counters == {
        "fetched": 2,
        "matches": 2,
        "misses": 0,
        "placed": 2,
        "repaired": 0,
        "uploaded": 0,
    }
    assert {activity["title"] for activity in store.activities} == {
        "NFO mismatch",
        "Verification timed out",
    }


@pytest.mark.asyncio
async def test_modes_disabled_connector_and_dry_run_gate_mutations(
    tmp_path: Path,
) -> None:
    snapshot = torrent_snapshot("dry", "Release.Dry-GROUP")

    off_qbit = FakeQBit([snapshot], {"dry": torrent_files(snapshot.name)})
    off = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path, mode=DownloadMode.OFF),
        store=FakeRuntimeStore(),
        qbit=off_qbit,
    )
    off_outcome = await off.scan_and_repair()

    disabled_qbit = FakeQBit([snapshot], {"dry": torrent_files(snapshot.name)})
    disabled = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path, qbit_enabled=False),
        store=FakeRuntimeStore(),
        qbit=disabled_qbit,
    )
    disabled_outcome = await disabled.scan_and_repair()

    dry_qbit = FakeQBit([snapshot], {"dry": torrent_files(snapshot.name)})
    dry_crowdnfo = FakeCrowdNFO({snapshot.name: RAW_NFO})
    data_root = tmp_path / "data"
    dry_repair = TorrentRepairService(
        crowdnfo=dry_crowdnfo,
        qbit=dry_qbit,
        path_mapper=PathMapper(
            mappings=[PathMapping(remote_root="/data", local_root=data_root)],
            allowed_roots=[data_root],
        ),
        dry_run=True,
    )
    dry_store = FakeRuntimeStore()
    dry = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path, dry_run=True),
        store=dry_store,
        qbit=dry_qbit,
        repair_service=dry_repair,
    )
    dry_outcome = await dry.scan_and_repair()

    assert off_outcome.status == "skipped" and off_qbit.calls == []
    assert disabled_outcome.status == "skipped" and disabled_qbit.calls == []
    assert dry_outcome.status == "dry_run"
    assert dry_crowdnfo.download_calls == []
    assert not {"priority", "recheck", "resume"} & {call[0] for call in dry_qbit.calls}
    assert dry_store.counters["repaired"] == 0


@pytest.mark.asyncio
async def test_library_backfill_fetches_raw_sidecars_for_radarr_and_sonarr(
    tmp_path: Path,
) -> None:
    movie = tmp_path / "data/movies/Movie/Movie.mkv"
    episode = tmp_path / "data/series/Show/Show.S01E01.mkv"
    movie.parent.mkdir(parents=True)
    episode.parent.mkdir(parents=True)
    movie.write_bytes(b"movie")
    episode.write_bytes(b"episode")
    movie_item = LibraryMediaItem("Movie.2026-GROUP", movie, source="radarr")
    episode_item = LibraryMediaItem("Show.S01E01-GROUP", episode, source="sonarr")
    crowdnfo = FakeCrowdNFO(
        {movie_item.release_name: RAW_NFO, episode_item.release_name: b"tv\r\n\xff"}
    )
    store = FakeRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(
            tmp_path,
            radarr_enabled=True,
            sonarr_enabled=True,
        ),
        store=store,
        crowdnfo=crowdnfo,
        library_connectors={
            "radarr": FakeLibraryConnector([movie_item]),
            "sonarr": FakeLibraryConnector([episode_item]),
        },
    )

    outcome = await runtime.scan_libraries()

    assert outcome.status == "success"
    assert movie.with_suffix(".nfo").read_bytes() == RAW_NFO
    assert episode.with_suffix(".nfo").read_bytes() == b"tv\r\n\xff"
    assert crowdnfo.download_calls == [
        (movie_item.release_name, None),
        (episode_item.release_name, None),
    ]
    assert store.counters["fetched"] == 2
    assert store.counters["matches"] == 2


@pytest.mark.asyncio
async def test_sab_completion_isolates_live_in_failure_from_contribution(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    live = FakeSABLiveService()
    webhook = SABWebhookHandler(
        live_service=live,
        fetch_enabled=True,
        contribute_enabled=True,
    )
    store = FakeRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(
            tmp_path,
            mode=DownloadMode.NEW_ONLY,
            sab_enabled=True,
            contribute=True,
        ),
        store=store,
        sab_webhook=webhook,
    )
    event = SABCompletionEvent(
        release_name="Movie.2026-GROUP",
        storage_path="/data/downloads/Movie.2026-GROUP",
        category="movies",
    )

    outcome = await runtime.handle_sab_completion(event)

    assert [name for name, _ in live.calls] == ["fetch", "contribute"]
    assert outcome.status == "partial"
    assert outcome.result.accepted is True
    assert outcome.result.errors == {"fetch": "connection failed"}
    assert store.counters["fetched"] == 0
    assert store.counters["uploaded"] == 1
    assert store.jobs["job-1"]["status"] == "partial"
    assert "must-not-leak" not in caplog.text


@pytest.mark.asyncio
async def test_dashboard_snapshot_combines_health_counters_activity_and_stuck(
    tmp_path: Path,
) -> None:
    stuck = torrent_snapshot("dashboard", "Release.Dashboard-GROUP")
    qbit = FakeQBit([stuck], {stuck.torrent_hash: torrent_files(stuck.name)})
    store = FakeRuntimeStore()
    store.counters.update(
        fetched=4,
        matches=3,
        misses=1,
        placed=3,
        repaired=2,
        uploaded=5,
    )
    await store.record_activity(
        activity_type="repair",
        status="success",
        title="Torrent repaired",
        message=stuck.name,
    )
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path, radarr_enabled=True),
        store=store,
        qbit=qbit,
        health_connectors={
            "crowdnfo": FakeHealthConnector(ConnectorHealth(True, version="beta")),
            "qbittorrent": qbit,
            "radarr": FakeHealthConnector(
                ConnectorHealth(False, detail="connection failed")
            ),
        },
    )

    snapshot = await runtime.dashboard_snapshot()

    health = {connector.id: connector for connector in snapshot.connectors}
    assert health["crowdnfo"].status == "healthy"
    assert health["qbittorrent"].status == "healthy"
    assert health["radarr"].status == "unhealthy"
    assert snapshot.counters.fetched == 4
    assert snapshot.counters.placed == 3
    assert snapshot.counters.repaired == 2
    assert snapshot.recent_activity[0].title == "Torrent repaired"
    assert len(snapshot.stuck_torrents) == 1
    assert snapshot.stuck_torrents[0].hash == "dashboard"
    assert snapshot.stuck_torrents[0].missing_nfo_path.endswith(".nfo")


@pytest.mark.asyncio
async def test_dashboard_groups_nfos_and_exposes_every_incomplete_torrent(
    tmp_path: Path,
) -> None:
    repairable = torrent_snapshot("ready", "Release.Ready-GROUP")
    repairable_files = torrent_files(repairable.name)
    repairable_files.insert(
        1,
        TorrentFile(
            index=9,
            path=f"{repairable.name}/{repairable.name}.proof.nfo",
            size=4_096,
            progress=0.0,
            priority=0,
        ),
    )
    video_incomplete = torrent_snapshot("video", "Release.Video-GROUP")
    video_files = torrent_files(video_incomplete.name)
    video_files[1] = TorrentFile(
        index=8,
        path=f"{video_incomplete.name}/{video_incomplete.name}.mkv",
        size=8_000_000_000,
        progress=0.75,
        priority=1,
    )
    no_nfo = torrent_snapshot("no-nfo", "Release.NoNfo-GROUP")
    no_nfo_files = [torrent_files(no_nfo.name)[1]]
    complete = torrent_snapshot(
        "complete",
        "Release.Complete-GROUP",
        progress=1.0,
        state="stalledUP",
    )
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=FakeRuntimeStore(),
        qbit=FakeQBit(
            [repairable, video_incomplete, no_nfo, complete],
            {
                repairable.torrent_hash: repairable_files,
                video_incomplete.torrent_hash: video_files,
                no_nfo.torrent_hash: no_nfo_files,
                complete.torrent_hash: torrent_files(complete.name, nfo_progress=1.0),
            },
        ),
    )

    snapshot = await runtime.dashboard_snapshot()

    torrents = {torrent.hash: torrent for torrent in snapshot.stuck_torrents}
    assert set(torrents) == {"ready", "video", "no-nfo"}
    assert torrents["ready"].repairable is True
    assert torrents["ready"].missing_nfo_count == 2
    assert torrents["ready"].reason == "ready"
    assert torrents["ready"].repair_outcome == "ready"
    assert torrents["video"].repairable is False
    assert torrents["video"].missing_nfo_count == 1
    assert torrents["video"].reason == "video_incomplete"
    assert torrents["no-nfo"].repairable is False
    assert torrents["no-nfo"].missing_nfo_count == 0
    assert torrents["no-nfo"].reason == "no_incomplete_nfo"


@pytest.mark.asyncio
async def test_actions_enqueue_tasks_and_return_stable_job_ids(tmp_path: Path) -> None:
    queue = FakeActionQueue()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=FakeRuntimeStore(),
        action_queue=queue,
    )

    scan = await runtime.enqueue_scan_and_repair()
    repair = await runtime.enqueue_repair_torrent("deadbeef")

    assert scan.job_id == "queued-1"
    assert repair.job_id == "queued-2"
    assert queue.calls == [
        ("scan_and_repair", {}),
        ("repair_torrent", {"torrent_hash": "deadbeef"}),
    ]


@pytest.mark.asyncio
async def test_connector_outage_is_skipped_and_sanitized_without_aborting_job(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    qbit = FakeQBit(
        [],
        {},
        list_error=ConnectionError("offline; password=must-not-leak"),
    )
    store = FakeRuntimeStore()
    runtime = CrowdarrrRuntime(
        settings=runtime_settings(tmp_path),
        store=store,
        qbit=qbit,
    )

    outcome = await runtime.scan_and_repair()

    assert outcome.status == "skipped"
    assert store.jobs["job-1"]["status"] == "skipped"
    assert store.activities[-1]["status"] == "warning"
    assert store.activities[-1]["message"] == "connection failed"
    assert "qbittorrent" in caplog.text.lower()
    assert "unavailable" in caplog.text.lower()
    assert "must-not-leak" not in caplog.text
