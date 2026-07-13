"""Application-level orchestration for scans, repair, dashboard, and actions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast
from uuid import uuid4

import httpx

from backend.connectors.health import (
    ConnectorHealth,
    ConnectorSupervisor,
    sanitized_error,
)
from backend.connectors.qbit import (
    VIDEO_SUFFIXES,
    MissingNFO,
    TorrentFile,
    TorrentSnapshot,
    find_stuck_nfos,
)
from backend.connectors.sab import SABCompletionEvent, SABWebhookResult
from backend.core.contribution import ContributionItem
from backend.core.files import atomic_write_bytes
from backend.core.hashing import AsyncHashService as AsyncHashService
from backend.core.hashing import HashResult
from backend.core.library import LibraryMediaItem, find_missing_sidecars
from backend.core.repair import RepairResult, RepairStatus
from backend.core.scan import ScanTrigger, mode_allows_trigger
from backend.core.settings import AppSettings
from backend.crowdnfo.client import UnsupportedLookupError
from backend.db.operations import OperationsStore

LOGGER = logging.getLogger(__name__)

WorkflowStatus = Literal["success", "partial", "failed", "skipped", "dry_run"]
ConnectorStatus = Literal["healthy", "unhealthy", "degraded", "disabled", "unknown"]

_COUNTER_NAMES = ("fetched", "matches", "misses", "repaired", "uploaded")
_CONNECTOR_LABELS = {
    "crowdnfo": "CrowdNFO",
    "qbittorrent": "qBittorrent",
    "sabnzbd": "SABnzbd",
    "radarr": "Radarr",
    "sonarr": "Sonarr",
    "umlautadaptarr": "UmlautAdaptarr",
}


def _is_empty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size == 0
    except OSError:
        return False


class RuntimeStore(Protocol):
    async def start_job(self, *, action: str, target: str | None = None) -> str: ...

    async def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        detail: str | None = None,
    ) -> object: ...

    async def increment_counter(self, name: str, amount: int = 1) -> object: ...

    async def record_activity(
        self,
        *,
        activity_type: str,
        status: str,
        title: str,
        message: str,
        miss_id: str | None = None,
    ) -> object: ...

    async def record_miss(
        self,
        *,
        source: str,
        release_name: str,
        reason: str,
        retryable: bool,
    ) -> str: ...

    async def get_counters(self) -> Mapping[str, int]: ...

    async def recent_activity(
        self, *, limit: int
    ) -> Sequence[Mapping[str, object]]: ...

    async def was_completed(self, key: str) -> bool: ...

    async def mark_completed(self, key: str) -> None: ...


class QBitRuntimeConnector(Protocol):
    async def list_torrents(self) -> list[TorrentSnapshot]: ...

    async def list_files(self, torrent_hash: str) -> list[TorrentFile]: ...

    async def get_torrent(self, torrent_hash: str) -> TorrentSnapshot: ...


class RepairService(Protocol):
    async def repair(self, candidate: MissingNFO) -> RepairResult: ...


class CrowdNFODownloader(Protocol):
    async def download_nfo(
        self,
        *,
        release_name: str,
        media_sha256: str | None = None,
    ) -> bytes: ...


class StrategyAwareCrowdNFODownloader:
    """Enforce matching mode without pretending a hash-only route exists."""

    def __init__(
        self,
        *,
        client: CrowdNFODownloader,
        mode: str,
    ) -> None:
        if mode not in {
            "hash_then_release_name",
            "hash_only",
            "release_name_only",
        }:
            raise ValueError("unsupported CrowdNFO matching mode")
        self._client = client
        self._mode = mode

    async def download_nfo(
        self,
        *,
        release_name: str,
        media_sha256: str | None = None,
    ) -> bytes:
        if self._mode == "release_name_only":
            return await self._client.download_nfo(
                release_name=release_name,
                media_sha256=None,
            )
        if self._mode == "hash_only":
            if media_sha256 is None:
                raise LookupError("media hash is unavailable")
            raise UnsupportedLookupError(
                "hash-only CrowdNFO lookup is not available in the current API"
            )
        if media_sha256 is not None:
            LOGGER.info(
                "CrowdNFO hash-only lookup is unavailable; "
                "using verified release-name fallback"
            )
        return await self._client.download_nfo(
            release_name=release_name,
            media_sha256=media_sha256,
        )


class LibraryConnector(Protocol):
    async def scan(self) -> list[LibraryMediaItem]: ...


class SABWebhook(Protocol):
    async def handle(self, event: SABCompletionEvent) -> SABWebhookResult: ...


class SABHistory(Protocol):
    async def list_completed(self) -> list[SABCompletionEvent]: ...


class HashService(Protocol):
    async def hash_file(self, path: Path) -> HashResult: ...


class LivePathMapper(Protocol):
    def map_path(self, reported_path: str | PurePosixPath) -> Path: ...


class ContributionRunner(Protocol):
    async def contribute(
        self,
        item: ContributionItem,
        *,
        include_nfo: bool,
        include_mediainfo: bool,
        include_filelist: bool,
    ) -> object: ...


class QBitLiveService(Protocol):
    async def fetch_missing(self, torrent: TorrentSnapshot) -> object: ...

    async def contribute(self, torrent: TorrentSnapshot) -> object: ...


class CompletionStore(Protocol):
    async def was_completed(self, key: str) -> bool: ...

    async def mark_completed(self, key: str) -> None: ...


class HealthConnector(Protocol):
    async def healthcheck(self) -> ConnectorHealth: ...


class ActionQueue(Protocol):
    async def enqueue(self, *, action: str, payload: dict[str, str]) -> str: ...


class ActionQueueFull(RuntimeError):  # noqa: N818 - public queue contract
    """Raised when a bounded action queue cannot accept more pending work."""


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    job_id: str
    status: WorkflowStatus
    result: object | None = None


@dataclass(frozen=True, slots=True)
class QueuedAction:
    job_id: str
    status: str = "accepted"


@dataclass(frozen=True, slots=True)
class DashboardConnector:
    id: str
    name: str
    status: ConnectorStatus
    message: str
    latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DashboardCounters:
    fetched: int = 0
    matches: int = 0
    misses: int = 0
    repaired: int = 0
    uploaded: int = 0


@dataclass(frozen=True, slots=True)
class DashboardActivity:
    id: str
    type: str
    title: str
    message: str
    status: str
    created_at: str
    miss_id: str | None = None


@dataclass(frozen=True, slots=True)
class DashboardStuckTorrent:
    hash: str
    name: str
    category: str
    progress: float
    missing_nfo_path: str


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    connectors: tuple[DashboardConnector, ...]
    counters: DashboardCounters
    dry_run: bool
    recent_activity: tuple[DashboardActivity, ...]
    stuck_torrents: tuple[DashboardStuckTorrent, ...]


@dataclass(frozen=True, slots=True)
class _StuckRecord:
    torrent: TorrentSnapshot
    candidate: MissingNFO


@dataclass(slots=True)
class _RepairSummary:
    successes: int = 0
    failures: int = 0
    dry_runs: int = 0

    @property
    def status(self) -> WorkflowStatus:
        if self.failures and self.successes:
            return "partial"
        if self.failures:
            return "failed"
        if self.dry_runs and not self.successes:
            return "dry_run"
        if self.successes:
            return "success"
        return "skipped"


class OperationsRuntimeStore:
    """Adapt ``OperationsStore`` to the runtime's persistence boundary."""

    records_miss_activity = True

    def __init__(self, operations: OperationsStore) -> None:
        self._operations = operations

    async def start_job(self, *, action: str, target: str | None = None) -> str:
        job_id = f"{action.replace('_', '-')}-{uuid4().hex}"
        await self._operations.create_job(
            job_id=job_id,
            kind=action,
            status="running",
        )
        if target is not None:
            await self._operations.record_activity(
                event_type="job_started",
                message=f"{action.replace('_', ' ')} started",
                details={"job_id": job_id, "target": target, "status": "info"},
            )
        return job_id

    async def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        detail: str | None = None,
    ) -> object:
        result = {"detail": detail} if detail is not None else {}
        return await self._operations.update_job(job_id, status=status, result=result)

    async def increment_counter(self, name: str, amount: int = 1) -> object:
        return await self._operations.increment_counter(name, amount)

    async def record_activity(
        self,
        *,
        activity_type: str,
        status: str,
        title: str,
        message: str,
        miss_id: str | None = None,
    ) -> object:
        details: dict[str, object] = {"status": status, "title": title}
        if miss_id is not None:
            details["miss_id"] = miss_id
        return await self._operations.record_activity(
            event_type=activity_type,
            message=message,
            details=details,
        )

    async def record_miss(
        self,
        *,
        source: str,
        release_name: str,
        reason: str,
        retryable: bool,
    ) -> str:
        miss_id = f"miss-{uuid4().hex}"
        await self._operations.record_activity(
            event_type="miss",
            message=reason,
            details={
                "miss_id": miss_id,
                "source": source,
                "release_name": release_name,
                "retryable": retryable,
                "status": "warning",
                "title": "CrowdNFO miss",
            },
        )
        return miss_id

    async def get_counters(self) -> Mapping[str, int]:
        return await self._operations.get_counters()

    async def recent_activity(self, *, limit: int) -> Sequence[Mapping[str, object]]:
        records = await self._operations.list_activity(limit=limit)
        return [
            {
                "id": str(record.id),
                "type": record.event_type,
                "title": str(
                    record.details.get(
                        "title", record.event_type.replace("_", " ").title()
                    )
                ),
                "message": record.message,
                "status": str(record.details.get("status", "info")),
                "created_at": record.created_at.isoformat(),
                **(
                    {"miss_id": str(record.details["miss_id"])}
                    if record.details.get("miss_id") is not None
                    else {}
                ),
            }
            for record in records
        ]

    async def was_completed(self, key: str) -> bool:
        return await self._operations.was_completed(key)

    async def mark_completed(self, key: str) -> None:
        await self._operations.mark_completed(key)


@dataclass(frozen=True, slots=True)
class _ActionRequest:
    action: str
    payload: dict[str, str]
    job_id: str
    dedupe_key: tuple[str, str]


class InProcessActionQueue:
    """Execute actions through a finite queue with active-key deduplication."""

    def __init__(
        self,
        *,
        store: RuntimeStore,
        max_concurrency: int = 2,
        max_pending: int = 64,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        if max_pending < 1:
            raise ValueError("max_pending must be at least one")
        self._store = store
        self._runtime: CrowdarrrRuntime | None = None
        self._max_concurrency = max_concurrency
        self._pending: asyncio.Queue[_ActionRequest] = asyncio.Queue(
            maxsize=max_pending
        )
        self._workers: set[asyncio.Task[None]] = set()
        self._active: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def bind(self, runtime: CrowdarrrRuntime) -> None:
        if self._runtime is not None and self._runtime is not runtime:
            raise RuntimeError("action queue is already bound")
        self._runtime = runtime

    @staticmethod
    def _dedupe_key(action: str, payload: Mapping[str, str]) -> tuple[str, str]:
        if action == "scan_and_repair":
            return action, "global"
        for field in ("torrent_hash", "miss_id"):
            value = payload.get(field)
            if value:
                return action, value
        return action, repr(sorted(payload.items()))

    def _start_workers(self) -> None:
        while len(self._workers) < self._max_concurrency:
            index = len(self._workers) + 1
            worker = asyncio.create_task(
                self._worker(),
                name=f"crowdarrr-action-worker-{index}",
            )
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)

    async def _worker(self) -> None:
        while True:
            request = await self._pending.get()
            try:
                runtime = self._runtime
                if runtime is None:
                    raise RuntimeError("action queue is not bound")
                await runtime.run_queued_action(
                    action=request.action,
                    payload=request.payload,
                    job_id=request.job_id,
                )
            except asyncio.CancelledError:
                await self._store.finish_job(
                    request.job_id,
                    status="failed",
                    detail="application shutdown",
                )
                raise
            except Exception as error:
                safe_error = sanitized_error(error)
                LOGGER.exception("queued runtime action failed (%s)", safe_error)
                await self._store.finish_job(
                    request.job_id,
                    status="failed",
                    detail=safe_error,
                )
            finally:
                async with self._lock:
                    if self._active.get(request.dedupe_key) == request.job_id:
                        self._active.pop(request.dedupe_key, None)
                self._pending.task_done()

    async def enqueue(self, *, action: str, payload: dict[str, str]) -> str:
        async with self._lock:
            if self._closed:
                raise RuntimeError("action queue is closed")
            if self._runtime is None:
                raise RuntimeError("action queue is not bound")
            dedupe_key = self._dedupe_key(action, payload)
            existing = self._active.get(dedupe_key)
            if existing is not None:
                return existing
            if self._pending.full():
                raise ActionQueueFull("action queue is full")
            target = payload.get("torrent_hash") or payload.get("miss_id")
            job_id = await self._store.start_job(action=action, target=target)
            self._active[dedupe_key] = job_id
            self._pending.put_nowait(
                _ActionRequest(action, dict(payload), job_id, dedupe_key)
            )
            self._start_workers()
            return job_id

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            workers = tuple(self._workers)
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        while not self._pending.empty():
            request = self._pending.get_nowait()
            await self._store.finish_job(
                request.job_id,
                status="failed",
                detail="application shutdown",
            )
            self._active.pop(request.dedupe_key, None)
            self._pending.task_done()


class SABLiveWorkflow:
    """Hash-aware live-in/live-out processing shared by SAB and qBittorrent."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        path_mapper: LivePathMapper,
        crowdnfo: CrowdNFODownloader,
        contribution: ContributionRunner,
        hash_service: HashService | None = None,
    ) -> None:
        self._settings = settings
        self._path_mapper = path_mapper
        self._crowdnfo = crowdnfo
        self._contribution = contribution
        self._hash_service = hash_service
        self._allowed_roots = tuple(
            Path(mapping.local_root) for mapping in settings.path_mappings
        )

    def _inspect_release(
        self, event: SABCompletionEvent
    ) -> tuple[Path, Path, Path | None, list[dict[str, object]]]:
        reported = self._path_mapper.map_path(event.remote_storage_path)
        if reported.is_file():
            root = reported.parent
            media = reported
        elif reported.is_dir():
            root = reported
            root_resolved = root.resolve(strict=True)
            media_candidates = [
                candidate
                for candidate in root.rglob("*")
                if candidate.is_file()
                and candidate.suffix.casefold() in VIDEO_SUFFIXES
                and candidate.resolve(strict=True).is_relative_to(root_resolved)
            ]
            if not media_candidates:
                raise FileNotFoundError("completed download contains no media file")
            media = max(
                media_candidates,
                key=lambda candidate: candidate.stat().st_size,
            )
        else:
            raise FileNotFoundError("completed download path is unavailable")

        root_resolved = root.resolve(strict=True)
        preferred_nfo = media.with_suffix(".nfo")
        nfo_path: Path | None = preferred_nfo if preferred_nfo.is_file() else None
        if nfo_path is None:
            nfo_candidates = sorted(
                candidate
                for candidate in root.rglob("*")
                if candidate.is_file()
                and candidate.suffix.casefold() == ".nfo"
                and candidate.resolve(strict=True).is_relative_to(root_resolved)
            )
            nfo_path = nfo_candidates[0] if nfo_candidates else None

        filelist = [
            {
                "file_path": path.relative_to(root).as_posix(),
                "file_size_bytes": path.stat().st_size,
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and path.resolve(strict=True).is_relative_to(root_resolved)
        ]
        return root, media, nfo_path, filelist

    async def _media_digest(
        self,
        media: Path,
        *,
        match_lookup: bool,
    ) -> str | None:
        if match_lookup and self._settings.match_strategy == "release_name_only":
            return None
        if self._hash_service is None:
            if match_lookup and self._settings.match_strategy == "hash_only":
                raise LookupError("media hashing is unavailable")
            return None
        try:
            result = await self._hash_service.hash_file(media)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if match_lookup and self._settings.match_strategy == "hash_only":
                raise LookupError("media hashing failed") from error
            LOGGER.info("media hashing unavailable (%s)", sanitized_error(error))
            return None
        if (
            result.digest is None
            and match_lookup
            and self._settings.match_strategy == "hash_only"
        ):
            raise LookupError(result.skipped_reason or "media hash unavailable")
        return result.digest

    async def fetch_missing(self, event: SABCompletionEvent) -> object:
        _, media, nfo_path, _ = await asyncio.to_thread(self._inspect_release, event)
        if nfo_path is not None:
            nfo_size = await asyncio.to_thread(lambda: nfo_path.stat().st_size)
            if nfo_size > 0:
                return nfo_path
        target = nfo_path or media.with_suffix(".nfo")
        if self._settings.dry_run:
            return target
        overwrite_empty = await asyncio.to_thread(_is_empty_file, target)
        media_sha256 = await self._media_digest(media, match_lookup=True)
        payload = await self._crowdnfo.download_nfo(
            release_name=event.release_name,
            media_sha256=media_sha256,
        )
        if not payload:
            raise ValueError("downloaded nfo is empty")
        return await asyncio.to_thread(
            atomic_write_bytes,
            target,
            payload,
            allowed_roots=self._allowed_roots,
            overwrite=overwrite_empty,
        )

    async def contribute(self, event: SABCompletionEvent) -> object:
        _, media, nfo_path, filelist = await asyncio.to_thread(
            self._inspect_release, event
        )
        if self._settings.dry_run:
            return None
        media_sha256 = await self._media_digest(media, match_lookup=False)
        return await self._contribution.contribute(
            ContributionItem(
                release_name=event.release_name,
                media_path=media,
                nfo_path=nfo_path,
                source_category=event.category,
                media_sha256=media_sha256,
                filelist=filelist,
            ),
            include_nfo=self._settings.contribute.nfo,
            include_mediainfo=self._settings.contribute.mediainfo,
            include_filelist=self._settings.contribute.filelist,
        )


class QBitLiveWorkflow:
    """Adapt qBittorrent completion records to the shared live workflow."""

    def __init__(self, workflow: SABLiveWorkflow) -> None:
        self._workflow = workflow

    @staticmethod
    def _event(torrent: TorrentSnapshot) -> SABCompletionEvent:
        return SABCompletionEvent(
            release_name=torrent.name,
            storage_path=torrent.content_path,
            category=torrent.category,
            nzo_id=torrent.torrent_hash,
        )

    async def fetch_missing(self, torrent: TorrentSnapshot) -> object:
        return await self._workflow.fetch_missing(self._event(torrent))

    async def contribute(self, torrent: TorrentSnapshot) -> object:
        return await self._workflow.contribute(self._event(torrent))


class QBitCompletedPoller:
    """Process completed torrents once for independently enabled live actions."""

    def __init__(
        self,
        *,
        qbit: QBitRuntimeConnector,
        live_service: QBitLiveService,
        store: CompletionStore,
        fetch_enabled: bool,
        contribute_enabled: bool,
        poll_interval: float = 30.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._qbit = qbit
        self._live_service = live_service
        self._store = store
        self._fetch_enabled = fetch_enabled
        self._contribute_enabled = contribute_enabled
        self._poll_interval = poll_interval
        self._poll_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @staticmethod
    def _key(torrent: TorrentSnapshot) -> str:
        return f"qbit-completion:{torrent.torrent_hash}"

    async def poll_once(self) -> dict[str, SABWebhookResult]:
        results: dict[str, SABWebhookResult] = {}
        async with self._poll_lock:
            torrents = await self._qbit.list_torrents()
            for torrent in torrents:
                if torrent.progress < 1 or torrent.state.casefold().startswith(
                    "checking"
                ):
                    continue
                key = self._key(torrent)
                if await self._store.was_completed(key):
                    continue
                actions: list[str] = []
                errors: dict[str, str] = {}
                operations = (
                    ("fetch", self._fetch_enabled, self._live_service.fetch_missing),
                    (
                        "contribute",
                        self._contribute_enabled,
                        self._live_service.contribute,
                    ),
                )
                for name, enabled, operation in operations:
                    if not enabled:
                        continue
                    actions.append(name)
                    try:
                        await operation(torrent)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        errors[name] = sanitized_error(error)
                        LOGGER.warning(
                            "qBittorrent %s completion step failed (%s)",
                            name,
                            errors[name],
                        )
                result = SABWebhookResult(
                    accepted=True,
                    actions=tuple(actions),
                    errors=errors,
                )
                results[torrent.torrent_hash] = result
                if actions and not errors:
                    await self._store.mark_completed(key)
        return results

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.info(
                    "qBittorrent completion polling unavailable (%s)",
                    sanitized_error(error),
                )
            await asyncio.sleep(self._poll_interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(),
                name="crowdarrr-qbit-completion-poller",
            )

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class CrowdarrrRuntime:
    """Compose connector operations into durable, user-facing workflows."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: RuntimeStore,
        qbit: QBitRuntimeConnector | None = None,
        repair_service: RepairService | None = None,
        crowdnfo: CrowdNFODownloader | None = None,
        library_connectors: Mapping[str, LibraryConnector] | None = None,
        sab_webhook: SABWebhook | None = None,
        sab_history: SABHistory | None = None,
        health_connectors: Mapping[str, HealthConnector] | None = None,
        action_queue: ActionQueue | None = None,
        hash_service: HashService | None = None,
        healthcheck_timeout: float = 5.0,
    ) -> None:
        if healthcheck_timeout <= 0:
            raise ValueError("healthcheck_timeout must be positive")
        self.settings = settings
        self._store = store
        self._qbit = qbit
        self._repair = repair_service
        self._crowdnfo = crowdnfo
        self._library_connectors = dict(library_connectors or {})
        self._sab_webhook = sab_webhook
        self._sab_history = sab_history
        self._health_connectors = dict(health_connectors or {})
        self._action_queue = action_queue
        self._hash_service = hash_service
        self._healthcheck_timeout = healthcheck_timeout

    async def _start_job(
        self,
        *,
        action: str,
        target: str | None = None,
        job_id: str | None = None,
    ) -> str:
        if job_id is not None:
            return job_id
        return await self._store.start_job(action=action, target=target)

    async def _finish(
        self,
        job_id: str,
        status: WorkflowStatus,
        *,
        result: object | None = None,
        detail: str | None = None,
    ) -> WorkflowOutcome:
        await self._store.finish_job(job_id, status=status, detail=detail)
        return WorkflowOutcome(job_id=job_id, status=status, result=result)

    async def _discover_stuck_records(self) -> list[_StuckRecord]:
        if self._qbit is None:
            return []
        records: list[_StuckRecord] = []
        torrents = await self._qbit.list_torrents()
        for torrent in torrents:
            if torrent.progress >= 1:
                continue
            files = await self._qbit.list_files(torrent.torrent_hash)
            hydrated = replace(torrent, files=files)
            records.extend(
                _StuckRecord(torrent=hydrated, candidate=candidate)
                for candidate in find_stuck_nfos(hydrated)
            )
        return records

    async def discover_stuck_torrents(self) -> list[MissingNFO]:
        return [record.candidate for record in await self._discover_stuck_records()]

    @staticmethod
    def _miss_reason(error: BaseException) -> str:
        if isinstance(error, LookupError):
            return "not found"
        if (
            isinstance(error, httpx.HTTPStatusError)
            and error.response.status_code == 404
        ):
            return "not found"
        return sanitized_error(error)

    async def _record_miss(
        self,
        *,
        source: str,
        release_name: str,
        reason: str,
        retryable: bool,
    ) -> None:
        await self._store.increment_counter("misses")
        miss_id = await self._store.record_miss(
            source=source,
            release_name=release_name,
            reason=reason,
            retryable=retryable,
        )
        if not bool(getattr(self._store, "records_miss_activity", False)):
            await self._store.record_activity(
                activity_type="miss",
                status="warning",
                title="CrowdNFO miss",
                message=reason,
                miss_id=miss_id,
            )

    async def _process_repair_candidates(
        self, candidates: Sequence[MissingNFO]
    ) -> _RepairSummary:
        summary = _RepairSummary()
        if self._repair is None:
            summary.failures = len(candidates)
            return summary

        grouped: dict[str, list[MissingNFO]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.torrent_hash, []).append(candidate)

        for group in grouped.values():
            results = await self._repair_group(group)
            group_verified = bool(results) and all(
                isinstance(result, RepairResult)
                and result.status is RepairStatus.SUCCESS
                for result in results
            )
            for candidate, result in zip(group, results, strict=True):
                if isinstance(result, Exception):
                    reason = self._miss_reason(result)
                    await self._record_miss(
                        source="qbittorrent",
                        release_name=candidate.torrent_name,
                        reason=reason,
                        retryable=True,
                    )
                    summary.failures += 1
                    continue

                if result.status is RepairStatus.SUCCESS:
                    for counter in ("fetched", "matches"):
                        await self._store.increment_counter(counter)
                    if not group_verified:
                        await self._store.record_activity(
                            activity_type="repair",
                            status="success",
                            title="Torrent NFO verified",
                            message=candidate.torrent_name,
                        )
                    summary.successes += 1
                elif result.status is RepairStatus.PLACED_UNVERIFIED:
                    for counter in ("fetched", "matches"):
                        await self._store.increment_counter(counter)
                    await self._store.record_activity(
                        activity_type="repair",
                        status="warning",
                        title="NFO placed; recheck disabled",
                        message=candidate.torrent_name,
                    )
                    summary.successes += 1
                elif result.status is RepairStatus.VERIFIED_INCOMPLETE:
                    for counter in ("fetched", "matches"):
                        await self._store.increment_counter(counter)
                    await self._store.record_activity(
                        activity_type="repair",
                        status="warning",
                        title="NFO verified; torrent incomplete",
                        message=result.message or candidate.torrent_name,
                    )
                    summary.successes += 1
                elif result.status is RepairStatus.TIMEOUT and result.verified:
                    for counter in ("fetched", "matches"):
                        await self._store.increment_counter(counter)
                    await self._store.record_activity(
                        activity_type="repair",
                        status="warning",
                        title="NFO verified; seeding not confirmed",
                        message=result.message or candidate.torrent_name,
                    )
                    summary.successes += 1
                elif result.status is RepairStatus.DRY_RUN:
                    await self._store.record_activity(
                        activity_type="repair",
                        status="info",
                        title="Dry-run repair",
                        message=candidate.torrent_name,
                    )
                    summary.dry_runs += 1
                else:
                    await self._record_miss(
                        source="qbittorrent",
                        release_name=candidate.torrent_name,
                        reason=(
                            "nfo mismatch"
                            if result.status is RepairStatus.MISMATCH
                            else "verification timed out"
                        ),
                        retryable=result.retryable,
                    )
                    summary.failures += 1

            if group_verified:
                await self._store.increment_counter("repaired")
                await self._store.record_activity(
                    activity_type="repair",
                    status="success",
                    title="Torrent repaired",
                    message=group[0].torrent_name,
                )
        return summary

    async def _repair_group(
        self,
        candidates: Sequence[MissingNFO],
    ) -> list[RepairResult | Exception]:
        if self._repair is None:
            return [RuntimeError("repair service is unavailable") for _ in candidates]
        batch_repair = getattr(self._repair, "repair_many", None)
        if callable(batch_repair):
            try:
                raw_results = await batch_repair(tuple(candidates))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                return [error for _ in candidates]
            if (
                not isinstance(raw_results, Sequence)
                or len(raw_results) != len(candidates)
                or any(not isinstance(result, RepairResult) for result in raw_results)
            ):
                invalid_result_error = RuntimeError(
                    "repair batch returned an invalid result set"
                )
                return [invalid_result_error for _ in candidates]
            return list(raw_results)

        results: list[RepairResult | Exception] = []
        for candidate in candidates:
            try:
                results.append(await self._repair.repair(candidate))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                results.append(error)
        return results

    def _qbit_scan_enabled(self) -> bool:
        return self.settings.qbittorrent.enabled and mode_allows_trigger(
            self.settings.download_mode,
            ScanTrigger.BACKFILL,
        )

    async def scan_and_repair(self, *, job_id: str | None = None) -> WorkflowOutcome:
        job_id = await self._start_job(action="scan_and_repair", job_id=job_id)
        if not self._qbit_scan_enabled() or self._qbit is None:
            return await self._finish(job_id, "skipped", detail="scan disabled")
        try:
            candidates = await self.discover_stuck_torrents()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            detail = sanitized_error(error)
            LOGGER.info("qbittorrent unavailable during scan (%s)", detail)
            await self._store.record_activity(
                activity_type="connector",
                status="warning",
                title="qBittorrent unavailable",
                message=detail,
            )
            return await self._finish(job_id, "skipped", detail=detail)
        if not candidates:
            return await self._finish(job_id, "success")
        if self._repair is None:
            return await self._finish(
                job_id,
                "skipped",
                detail="repair service unavailable",
            )
        summary = await self._process_repair_candidates(candidates)
        return await self._finish(job_id, summary.status)

    async def repair_torrent(
        self,
        torrent_hash: str,
        *,
        job_id: str | None = None,
    ) -> WorkflowOutcome:
        if not torrent_hash:
            raise ValueError("torrent hash cannot be blank")
        job_id = await self._start_job(
            action="repair_torrent",
            target=torrent_hash,
            job_id=job_id,
        )
        if not self.settings.qbittorrent.enabled or self._qbit is None:
            return await self._finish(job_id, "skipped", detail="connector disabled")
        if self._repair is None:
            return await self._finish(
                job_id,
                "skipped",
                detail="repair service unavailable",
            )
        try:
            torrent = await self._qbit.get_torrent(torrent_hash)
            candidates = find_stuck_nfos(torrent)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            detail = sanitized_error(error)
            LOGGER.info("qbittorrent unavailable during targeted repair (%s)", detail)
            return await self._finish(job_id, "skipped", detail=detail)
        if not candidates:
            return await self._finish(job_id, "skipped", detail="no missing nfo")
        summary = await self._process_repair_candidates(candidates)
        return await self._finish(job_id, summary.status)

    def _library_scan_enabled(self) -> bool:
        return mode_allows_trigger(
            self.settings.download_mode,
            ScanTrigger.BACKFILL,
        )

    def _library_connector_enabled(self, name: str) -> bool:
        connector_settings = getattr(self.settings, name, None)
        return bool(getattr(connector_settings, "enabled", False))

    @property
    def _allowed_roots(self) -> tuple[Path, ...]:
        return tuple(
            Path(mapping.local_root) for mapping in self.settings.path_mappings
        )

    async def _lookup_media_digest(self, media_path: Path) -> str | None:
        if self.settings.match_strategy == "release_name_only":
            return None
        if self._hash_service is None:
            if self.settings.match_strategy == "hash_only":
                raise LookupError("media hashing is unavailable")
            return None
        try:
            result = await self._hash_service.hash_file(media_path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.settings.match_strategy == "hash_only":
                raise LookupError("media hashing failed") from error
            LOGGER.info("media hashing unavailable (%s)", sanitized_error(error))
            return None
        if result.digest is None and self.settings.match_strategy == "hash_only":
            raise LookupError(result.skipped_reason or "media hash unavailable")
        return result.digest

    async def scan_libraries(self, *, job_id: str | None = None) -> WorkflowOutcome:
        job_id = await self._start_job(action="scan_libraries", job_id=job_id)
        if not self._library_scan_enabled():
            return await self._finish(job_id, "skipped", detail="backfill disabled")
        enabled = {
            name: connector
            for name, connector in self._library_connectors.items()
            if self._library_connector_enabled(name)
        }
        if not enabled or self._crowdnfo is None:
            return await self._finish(
                job_id,
                "skipped",
                detail="library connectors unavailable",
            )
        outcomes = await ConnectorSupervisor().run_all(
            operation="scan",
            connectors=cast(Mapping[str, object], enabled),
        )
        connector_failures = sum(outcome.skipped for outcome in outcomes.values())
        items: list[LibraryMediaItem] = []
        for outcome in outcomes.values():
            if not isinstance(outcome.value, list):
                continue
            items.extend(
                item for item in outcome.value if isinstance(item, LibraryMediaItem)
            )
        missing_items = find_missing_sidecars(items)
        successes = 0
        failures = connector_failures
        dry_runs = 0
        roots = self._allowed_roots
        for item in missing_items:
            if self.settings.dry_run:
                dry_runs += 1
                await self._store.record_activity(
                    activity_type="library_fetch",
                    status="info",
                    title="Dry-run library fetch",
                    message=item.release_name,
                )
                continue
            try:
                media_sha256 = await self._lookup_media_digest(item.local_media_path)
                payload = await self._crowdnfo.download_nfo(
                    release_name=item.release_name,
                    media_sha256=media_sha256,
                )
                if not payload:
                    raise ValueError("downloaded nfo is empty")
                if not roots:
                    raise ValueError("no allowed media roots are configured")
                overwrite_empty = await asyncio.to_thread(
                    _is_empty_file,
                    item.sidecar_path,
                )
                await asyncio.to_thread(
                    atomic_write_bytes,
                    item.sidecar_path,
                    payload,
                    allowed_roots=roots,
                    overwrite=overwrite_empty,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._record_miss(
                    source=item.source,
                    release_name=item.release_name,
                    reason=self._miss_reason(error),
                    retryable=True,
                )
                failures += 1
                continue
            for counter in ("fetched", "matches"):
                await self._store.increment_counter(counter)
            await self._store.record_activity(
                activity_type="library_fetch",
                status="success",
                title="Library NFO fetched",
                message=item.release_name,
            )
            successes += 1

        if failures and successes:
            status: WorkflowStatus = "partial"
        elif failures:
            status = "failed"
        elif dry_runs:
            status = "dry_run"
        else:
            status = "success"
        return await self._finish(job_id, status)

    @staticmethod
    def _sab_idempotency_key(event: SABCompletionEvent) -> str:
        nzo_id = event.nzo_id or "missing"
        return f"sab-completion:{nzo_id}:{event.storage_path.rstrip('/')}"

    async def _verify_sab_completion(self, event: SABCompletionEvent) -> bool:
        if self._sab_history is None:
            return True
        if event.nzo_id is None:
            return False
        completed = await self._sab_history.list_completed()
        expected_path = event.storage_path.rstrip("/")
        return any(
            candidate.nzo_id == event.nzo_id
            and candidate.storage_path.rstrip("/") == expected_path
            and candidate.release_name == event.release_name
            for candidate in completed
        )

    async def handle_sab_completion(
        self,
        event: SABCompletionEvent,
        *,
        job_id: str | None = None,
    ) -> WorkflowOutcome:
        job_id = await self._start_job(
            action="sab_completion",
            target=event.nzo_id or event.release_name,
            job_id=job_id,
        )
        live_in_enabled = mode_allows_trigger(
            self.settings.download_mode,
            ScanTrigger.NEW_DOWNLOAD,
        )
        if (
            not self.settings.sabnzbd.enabled
            or self._sab_webhook is None
            or (not live_in_enabled and not self.settings.contribute.enabled)
        ):
            return await self._finish(job_id, "skipped", detail="SAB workflow disabled")
        idempotency_key = self._sab_idempotency_key(event)
        verified_workflow = self._sab_history is not None
        try:
            if verified_workflow and await self._store.was_completed(idempotency_key):
                return await self._finish(
                    job_id,
                    "skipped",
                    detail="SAB completion was already processed",
                )
            if not await self._verify_sab_completion(event):
                return await self._finish(
                    job_id,
                    "failed",
                    detail="SAB completion did not match SABnzbd history",
                )
            result = await self._sab_webhook.handle(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            detail = sanitized_error(error)
            LOGGER.info("sabnzbd unavailable during completion handling (%s)", detail)
            return await self._finish(job_id, "skipped", detail=detail)

        successes = 0
        failures = 0
        for action in result.actions:
            action_error = result.errors.get(action)
            if action_error is not None:
                failures += 1
                await self._store.record_activity(
                    activity_type=f"sab_{action}",
                    status="warning",
                    title=f"SAB {action} failed",
                    message=action_error,
                )
                continue
            successes += 1
            if action == "fetch":
                for counter in ("fetched", "matches"):
                    await self._store.increment_counter(counter)
            elif action == "contribute":
                await self._store.increment_counter("uploaded")
            await self._store.record_activity(
                activity_type=f"sab_{action}",
                status="success",
                title=f"SAB {action} complete",
                message=event.release_name,
            )

        if failures and successes:
            status: WorkflowStatus = "partial"
        elif failures:
            status = "failed"
        elif successes:
            status = "success"
        else:
            status = "skipped"
        if verified_workflow and result.accepted and not result.errors:
            await self._store.mark_completed(idempotency_key)
        return await self._finish(job_id, status, result=result)

    def _connector_enabled(self, name: str) -> bool:
        if name == "crowdnfo":
            return name in self._health_connectors
        settings = getattr(self.settings, name, None)
        return bool(getattr(settings, "enabled", False))

    async def _connector_health_snapshot(
        self,
        connector_id: str,
        name: str,
    ) -> DashboardConnector:
        connector = self._health_connectors.get(connector_id)
        enabled = self._connector_enabled(connector_id)
        if connector is None:
            return DashboardConnector(
                id=connector_id,
                name=name,
                status="unhealthy" if enabled else "disabled",
                message="configuration incomplete" if enabled else "not configured",
            )
        if not enabled:
            return DashboardConnector(
                id=connector_id,
                name=name,
                status="disabled",
                message="not configured",
            )

        started = time.perf_counter()
        try:
            health = await asyncio.wait_for(
                connector.healthcheck(),
                timeout=self._healthcheck_timeout,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            health = ConnectorHealth(False, detail="healthcheck timeout")
        except Exception as error:
            detail = sanitized_error(error)
            LOGGER.info("%s unavailable during healthcheck (%s)", name, detail)
            health = ConnectorHealth(False, detail=detail)
        latency = max(0, round((time.perf_counter() - started) * 1_000))
        status: ConnectorStatus = "healthy" if health.healthy else "unhealthy"
        return DashboardConnector(
            id=connector_id,
            name=name,
            status=status,
            message=health.detail or health.version or status,
            latency_ms=latency,
        )

    async def _health_snapshot(self) -> tuple[DashboardConnector, ...]:
        return tuple(
            await asyncio.gather(
                *(
                    self._connector_health_snapshot(connector_id, name)
                    for connector_id, name in _CONNECTOR_LABELS.items()
                )
            )
        )

    @staticmethod
    def _activity_from_mapping(item: Mapping[str, object]) -> DashboardActivity:
        miss_id = item.get("miss_id")
        return DashboardActivity(
            id=str(item.get("id", "")),
            type=str(item.get("type", "activity")),
            title=str(item.get("title", "Activity")),
            message=str(item.get("message", "")),
            status=str(item.get("status", "info")),
            created_at=str(item.get("created_at", "")),
            miss_id=str(miss_id) if miss_id is not None else None,
        )

    async def dashboard_snapshot(self) -> DashboardSnapshot:
        health_task = asyncio.create_task(self._health_snapshot())
        counters_task = asyncio.create_task(self._store.get_counters())
        activity_task = asyncio.create_task(self._store.recent_activity(limit=50))
        try:
            stuck_records = (
                await self._discover_stuck_records()
                if self.settings.qbittorrent.enabled and self._qbit is not None
                else []
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            detail = sanitized_error(error)
            LOGGER.info("qbittorrent unavailable during dashboard scan (%s)", detail)
            stuck_records = []
        connectors, raw_counters, raw_activity = await asyncio.gather(
            health_task,
            counters_task,
            activity_task,
        )
        counters = {name: int(raw_counters.get(name, 0)) for name in _COUNTER_NAMES}
        return DashboardSnapshot(
            connectors=connectors,
            counters=DashboardCounters(**counters),
            dry_run=self.settings.dry_run,
            recent_activity=tuple(
                self._activity_from_mapping(item) for item in raw_activity
            ),
            stuck_torrents=tuple(
                DashboardStuckTorrent(
                    hash=record.torrent.torrent_hash,
                    name=record.torrent.name,
                    category=record.torrent.category,
                    progress=record.torrent.progress,
                    missing_nfo_path=str(record.candidate.reported_path),
                )
                for record in stuck_records
            ),
        )

    async def snapshot(self) -> DashboardSnapshot:
        """Dashboard-service alias used by the FastAPI service container."""

        return await self.dashboard_snapshot()

    async def enqueue_scan_and_repair(self) -> QueuedAction:
        if self._action_queue is None:
            raise RuntimeError("action queue is unavailable")
        return QueuedAction(
            await self._action_queue.enqueue(action="scan_and_repair", payload={})
        )

    async def enqueue_scheduled_backfill(self) -> QueuedAction:
        """Route scheduled work through the same bounded, deduplicated queue."""

        return await self.enqueue_scan_and_repair()

    async def enqueue_repair_torrent(self, torrent_hash: str) -> QueuedAction:
        if not torrent_hash:
            raise ValueError("torrent hash cannot be blank")
        if self._action_queue is None:
            raise RuntimeError("action queue is unavailable")
        return QueuedAction(
            await self._action_queue.enqueue(
                action="repair_torrent",
                payload={"torrent_hash": torrent_hash},
            )
        )

    async def enqueue_retry_miss(self, miss_id: str) -> QueuedAction:
        if not miss_id:
            raise ValueError("miss id cannot be blank")
        if self._action_queue is None:
            raise RuntimeError("action queue is unavailable")
        return QueuedAction(
            await self._action_queue.enqueue(
                action="retry_miss",
                payload={"miss_id": miss_id},
            )
        )

    async def full_backfill(self, *, job_id: str | None = None) -> WorkflowOutcome:
        """Run qBittorrent repair and library enrichment as one parent action."""

        job_id = await self._start_job(action="full_backfill", job_id=job_id)
        qbit_outcome = await self.scan_and_repair()
        library_outcome = await self.scan_libraries()
        statuses = {qbit_outcome.status, library_outcome.status}
        if "partial" in statuses or (
            "failed" in statuses and bool(statuses & {"success", "dry_run"})
        ):
            status: WorkflowStatus = "partial"
        elif "failed" in statuses:
            status = "failed"
        elif "success" in statuses:
            status = "success"
        elif "dry_run" in statuses:
            status = "dry_run"
        else:
            status = "skipped"
        return await self._finish(
            job_id,
            status,
            result={
                "qbittorrent_job_id": qbit_outcome.job_id,
                "library_job_id": library_outcome.job_id,
            },
        )

    async def run_queued_action(
        self,
        *,
        action: str,
        payload: Mapping[str, str],
        job_id: str,
    ) -> WorkflowOutcome:
        if action == "scan_and_repair":
            return await self.full_backfill(job_id=job_id)
        if action == "repair_torrent":
            torrent_hash = payload.get("torrent_hash")
            if not torrent_hash:
                return await self._finish(
                    job_id, "failed", detail="invalid job payload"
                )
            return await self.repair_torrent(torrent_hash, job_id=job_id)
        if action == "retry_miss":
            return await self.scan_and_repair(job_id=job_id)
        return await self._finish(job_id, "failed", detail="unknown action")
