"""Crowdarrr FastAPI application and single-container SPA entry point."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any, cast

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from backend.connectors.health import ConnectorHealth, sanitized_error
from backend.connectors.qbit import VIDEO_SUFFIXES, QBitConnector
from backend.connectors.radarr import RadarrConnector
from backend.connectors.sab import (
    SABCompletionEvent,
    SABnzbdConnector,
    SABWebhookHandler,
)
from backend.connectors.sonarr import SonarrConnector
from backend.connectors.umlaut import UmlautAdaptarrConnector
from backend.core.contribution import (
    ContributionItem,
    ContributionService,
    CrowdNFOUploader,
)
from backend.core.files import PathMapper, PathMapping, atomic_write_bytes
from backend.core.library import LibraryMediaItem
from backend.core.mediainfo import MediaInfoRunner
from backend.core.repair import TorrentRepairService
from backend.core.scan import ScanTrigger, mode_allows_trigger
from backend.core.scheduler import CrowdarrrScheduler
from backend.core.settings import AppSettings, DownloadMode
from backend.crowdnfo.client import CrowdNFOClient
from backend.db.operations import OperationsStore
from backend.db.settings import SettingsStore
from backend.runtime import (
    CrowdarrrRuntime,
    HealthConnector,
    InProcessActionQueue,
    OperationsRuntimeStore,
    WorkflowOutcome,
)

LOGGER = logging.getLogger(__name__)

_CONNECTOR_NAMES = frozenset(
    {"crowdnfo", "qbittorrent", "sabnzbd", "radarr", "sonarr", "umlautadaptarr"}
)
_COUNTER_NAMES = ("fetched", "repaired", "uploaded", "matches", "misses")


class SecurityMiddleware(BaseHTTPMiddleware):
    """Apply API bearer protection and browser hardening headers."""

    def __init__(self, app: Any, *, api_token: str | None) -> None:
        super().__init__(app)
        self._api_token = api_token or None

    def _authorized(self, request: Request) -> bool:
        authorization = request.headers.get("Authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        return bool(
            separator
            and scheme.casefold() == "bearer"
            and credential
            and self._api_token is not None
            and hmac.compare_digest(credential, self._api_token)
        )

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        protected = request.url.path.startswith("/api") and request.url.path != (
            "/api/health"
        )
        if self._api_token is not None and protected and not self._authorized(request):
            response: Response = JSONResponse(
                {"detail": "authentication required"}, status_code=401
            )
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=()"
        )
        return response


class _CrowdNFOHealth:
    """Probe the verified lookup route without requiring a private health API."""

    def __init__(self, client: CrowdNFOClient) -> None:
        self._client = client

    async def healthcheck(self) -> ConnectorHealth:
        try:
            await self._client.lookup(release_name="crowdarrr-healthcheck")
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return ConnectorHealth(True)
            return ConnectorHealth(False, detail=sanitized_error(error))
        except Exception as error:
            return ConnectorHealth(False, detail=sanitized_error(error))
        return ConnectorHealth(True)


class _ReleaseResolvingLibraryConnector:
    """Apply optional UmlautAdaptarr title recovery to library items."""

    def __init__(
        self,
        connector: RadarrConnector | SonarrConnector,
        umlaut: UmlautAdaptarrConnector,
    ) -> None:
        self._connector = connector
        self._umlaut = umlaut

    async def scan(self) -> list[LibraryMediaItem]:
        items = await self._connector.scan()
        resolved: list[LibraryMediaItem] = []
        for item in items:
            try:
                original = await self._umlaut.recover_release_name(item.release_name)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.info(
                    "umlautadaptarr unavailable during title recovery (%s)",
                    sanitized_error(error),
                )
                original = None
            resolved.append(
                replace(item, release_name=original) if original is not None else item
            )
        return resolved


class _DefaultSABLiveService:
    """Translate one SAB completion into live-in and live-out operations."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        path_mapper: PathMapper,
        crowdnfo: CrowdNFOClient,
        contribution: ContributionService,
    ) -> None:
        self._settings = settings
        self._path_mapper = path_mapper
        self._crowdnfo = crowdnfo
        self._contribution = contribution
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
            media_candidates = sorted(
                candidate
                for candidate in root.rglob("*")
                if candidate.is_file()
                and candidate.suffix.casefold() in VIDEO_SUFFIXES
                and candidate.resolve(strict=True).is_relative_to(root_resolved)
            )
            if not media_candidates:
                raise FileNotFoundError("completed SAB job contains no media file")
            media = media_candidates[0]
        else:
            raise FileNotFoundError("completed SAB storage path is unavailable")

        preferred_nfo = media.with_suffix(".nfo")
        nfo_path: Path | None = preferred_nfo if preferred_nfo.is_file() else None
        if nfo_path is None:
            nfo_candidates = sorted(
                candidate
                for candidate in root.rglob("*.nfo")
                if candidate.is_file()
                and candidate.resolve(strict=True).is_relative_to(
                    root.resolve(strict=True)
                )
            )
            nfo_path = nfo_candidates[0] if nfo_candidates else None

        filelist = [
            {
                "file_path": path.relative_to(root).as_posix(),
                "file_size_bytes": path.stat().st_size,
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
        ]
        return root, media, nfo_path, filelist

    async def fetch_missing(self, event: SABCompletionEvent) -> object:
        _, media, nfo_path, _ = await asyncio.to_thread(self._inspect_release, event)
        if nfo_path is not None and nfo_path.stat().st_size > 0:
            return nfo_path
        target = media.with_suffix(".nfo")
        if self._settings.dry_run:
            return target
        payload = await self._crowdnfo.download_nfo(
            release_name=event.release_name,
            media_sha256=None,
        )
        if not payload:
            raise ValueError("downloaded nfo is empty")
        return await asyncio.to_thread(
            atomic_write_bytes,
            target,
            payload,
            allowed_roots=self._allowed_roots,
            overwrite=False,
        )

    async def contribute(self, event: SABCompletionEvent) -> object:
        _, media, nfo_path, filelist = await asyncio.to_thread(
            self._inspect_release, event
        )
        if self._settings.dry_run:
            return None
        return await self._contribution.contribute(
            ContributionItem(
                release_name=event.release_name,
                media_path=media,
                nfo_path=nfo_path,
                source_category=event.category,
                filelist=filelist,
            ),
            include_nfo=self._settings.contribute.nfo,
            include_mediainfo=self._settings.contribute.mediainfo,
            include_filelist=self._settings.contribute.filelist,
        )


@dataclass(slots=True)
class _RuntimeBundle:
    runtime: CrowdarrrRuntime
    queue: InProcessActionQueue
    health_connectors: dict[str, HealthConnector]
    closeables: tuple[object, ...]

    async def close(self) -> None:
        await self.queue.close()
        seen: set[int] = set()
        for closeable in self.closeables:
            if id(closeable) in seen:
                continue
            seen.add(id(closeable))
            close = getattr(closeable, "aclose", None)
            if not callable(close):
                continue
            result = close()
            if inspect.isawaitable(result):
                await result


class _RuntimeDashboard:
    def __init__(self, services: _DefaultServices) -> None:
        self._services = services

    async def snapshot(self) -> Any:
        runtime = self._services.runtime
        if runtime is None:
            counters = await self._services.operations.get_counters()
            return {
                "connectors": [],
                "counters": {name: counters.get(name, 0) for name in _COUNTER_NAMES},
                "recent_activity": [],
                "stuck_torrents": [],
            }
        return await runtime.dashboard_snapshot()


class _RuntimeActions:
    def __init__(self, services: _DefaultServices) -> None:
        self._services = services

    def _runtime(self) -> CrowdarrrRuntime:
        runtime = self._services.runtime
        if runtime is None:
            raise RuntimeError("runtime is not initialized")
        return runtime

    async def scan_repair(self) -> str:
        return (await self._runtime().enqueue_scan_and_repair()).job_id

    async def repair_torrent(self, torrent_hash: str) -> str:
        return (await self._runtime().enqueue_repair_torrent(torrent_hash)).job_id

    async def retry_miss(self, miss_id: str) -> str:
        return (await self._runtime().enqueue_retry_miss(miss_id)).job_id


class _RuntimeConnectors:
    def __init__(self, services: _DefaultServices) -> None:
        self._services = services

    async def test(self, connector: str) -> dict[str, Any]:
        settings = await self._services.settings.get()
        enabled = connector == "crowdnfo" or bool(
            getattr(getattr(settings, connector, None), "enabled", False)
        )
        if not enabled:
            return {
                "connector": connector,
                "status": "disabled",
                "latency_ms": None,
                "message": "connector is disabled",
            }
        bundle = self._services.bundle
        health_connector = (
            bundle.health_connectors.get(connector) if bundle is not None else None
        )
        if health_connector is None:
            return {
                "connector": connector,
                "status": "unhealthy",
                "latency_ms": None,
                "message": "connector configuration is incomplete",
            }
        started = asyncio.get_running_loop().time()
        health = await health_connector.healthcheck()
        latency = max(
            0,
            round((asyncio.get_running_loop().time() - started) * 1_000),
        )
        return {
            "connector": connector,
            "status": "healthy" if health.healthy else "unhealthy",
            "latency_ms": latency,
            "message": health.detail or health.version or "healthy",
        }


class _DefaultLogs:
    def __init__(self, operations: OperationsStore) -> None:
        self._operations = operations

    async def list(self, *, limit: int) -> dict[str, Any]:
        activity = await self._operations.list_activity(limit=limit)
        return {
            "items": [
                {
                    "id": str(item.id),
                    "timestamp": item.created_at.isoformat(),
                    "level": str(item.details.get("level", "info")),
                    "event": item.event_type,
                    "message": item.message,
                    "context": item.details,
                }
                for item in activity
            ],
            "next_cursor": None,
        }


class _DefaultServices:
    def __init__(self, *, settings: SettingsStore, operations: OperationsStore) -> None:
        self.settings = settings
        self.operations = operations
        self.store = OperationsRuntimeStore(operations)
        self.bundle: _RuntimeBundle | None = None
        self.dashboard = _RuntimeDashboard(self)
        self.actions = _RuntimeActions(self)
        self.connectors = _RuntimeConnectors(self)
        self.logs = _DefaultLogs(operations)
        self._reload_lock = asyncio.Lock()
        self._scheduler_backend: AsyncIOScheduler | None = None
        self._scheduler: CrowdarrrScheduler | None = None

    @property
    def runtime(self) -> CrowdarrrRuntime | None:
        return self.bundle.runtime if self.bundle is not None else None

    @staticmethod
    def _path_mapper(settings: AppSettings) -> PathMapper | None:
        if not settings.path_mappings:
            return None
        roots = [Path(mapping.local_root) for mapping in settings.path_mappings]
        return PathMapper(
            mappings=[
                PathMapping(
                    remote_root=mapping.remote_root,
                    local_root=Path(mapping.local_root),
                )
                for mapping in settings.path_mappings
            ],
            allowed_roots=roots,
        )

    @staticmethod
    def _url(value: object | None) -> str | None:
        return str(value).rstrip("/") if value is not None else None

    def _compose_bundle(self, settings: AppSettings) -> _RuntimeBundle:
        path_mapper = self._path_mapper(settings)
        closeables: list[object] = []
        health: dict[str, HealthConnector] = {}

        crowdnfo: CrowdNFOClient | None
        try:
            crowdnfo = CrowdNFOClient(
                base_url=str(settings.crowdnfo.base_url),
                api_key=settings.crowdnfo.api_key,
            )
        except Exception as error:
            LOGGER.warning("CrowdNFO runtime unavailable (%s)", sanitized_error(error))
            crowdnfo = None
        if crowdnfo is not None:
            closeables.append(crowdnfo)
            health["crowdnfo"] = _CrowdNFOHealth(crowdnfo)

        qbit: QBitConnector | None = None
        qbit_url = self._url(settings.qbittorrent.base_url)
        if settings.qbittorrent.enabled and qbit_url is not None:
            try:
                password = settings.qbittorrent.password.get_secret_value() or None
                qbit = QBitConnector(
                    base_url=qbit_url,
                    username=settings.qbittorrent.username,
                    password=password,
                    path_mapper=path_mapper,
                )
            except Exception as error:
                LOGGER.warning(
                    "qBittorrent runtime unavailable (%s)", sanitized_error(error)
                )
            else:
                closeables.append(qbit)
                health["qbittorrent"] = qbit

        sab: SABnzbdConnector | None = None
        sab_url = self._url(settings.sabnzbd.base_url)
        if settings.sabnzbd.enabled and sab_url is not None:
            try:
                sab = SABnzbdConnector(
                    base_url=sab_url,
                    api_key=settings.sabnzbd.api_key,
                )
            except Exception as error:
                LOGGER.warning(
                    "SABnzbd runtime unavailable (%s)", sanitized_error(error)
                )
            else:
                closeables.append(sab)
                health["sabnzbd"] = sab

        umlaut: UmlautAdaptarrConnector | None = None
        umlaut_url = self._url(settings.umlautadaptarr.base_url)
        if settings.umlautadaptarr.enabled and umlaut_url is not None:
            try:
                umlaut = UmlautAdaptarrConnector(base_url=umlaut_url)
            except Exception as error:
                LOGGER.warning(
                    "UmlautAdaptarr runtime unavailable (%s)",
                    sanitized_error(error),
                )
            else:
                closeables.append(umlaut)
                health["umlautadaptarr"] = umlaut

        library_connectors: dict[str, Any] = {}
        if path_mapper is not None:
            for name, connector_type in (
                ("radarr", RadarrConnector),
                ("sonarr", SonarrConnector),
            ):
                connector_settings = getattr(settings, name)
                base_url = self._url(connector_settings.base_url)
                api_key = connector_settings.api_key.get_secret_value()
                if not connector_settings.enabled or base_url is None or not api_key:
                    continue
                try:
                    connector = connector_type(
                        base_url=base_url,
                        api_key=api_key,
                        path_mapper=path_mapper,
                    )
                except Exception as error:
                    LOGGER.warning(
                        "%s runtime unavailable (%s)",
                        name,
                        sanitized_error(error),
                    )
                    continue
                closeables.append(connector)
                health[name] = connector
                library_connectors[name] = (
                    _ReleaseResolvingLibraryConnector(connector, umlaut)
                    if umlaut is not None
                    else connector
                )

        repair = (
            TorrentRepairService(
                crowdnfo=crowdnfo,
                qbit=qbit,
                path_mapper=path_mapper,
                allowed_roots=[
                    Path(mapping.local_root) for mapping in settings.path_mappings
                ],
                dry_run=settings.dry_run,
            )
            if crowdnfo is not None and qbit is not None and path_mapper is not None
            else None
        )

        sab_webhook: SABWebhookHandler | None = None
        if (
            sab is not None
            and crowdnfo is not None
            and path_mapper is not None
            and (
                mode_allows_trigger(
                    settings.download_mode,
                    ScanTrigger.NEW_DOWNLOAD,
                )
                or settings.contribute.enabled
            )
        ):
            mapping = {
                "radarr": "Movies",
                "sonarr": "TV",
                "movies": "Movies",
                "tv": "TV",
            }
            for source, destination in settings.category_mappings.items():
                if destination.casefold() in {
                    "movies",
                    "tv",
                    "games",
                    "software",
                    "music",
                    "books",
                    "audiobooks",
                    "other",
                    "unknown",
                }:
                    mapping[source] = destination
            contribution = ContributionService(
                crowdnfo=cast(CrowdNFOUploader, crowdnfo),
                mediainfo=MediaInfoRunner(),
                category_mapping=mapping,
            )
            live_service = _DefaultSABLiveService(
                settings=settings,
                path_mapper=path_mapper,
                crowdnfo=crowdnfo,
                contribution=contribution,
            )
            sab_webhook = SABWebhookHandler(
                live_service=live_service,
                fetch_enabled=mode_allows_trigger(
                    settings.download_mode,
                    ScanTrigger.NEW_DOWNLOAD,
                ),
                contribute_enabled=settings.contribute.enabled,
            )

        queue = InProcessActionQueue(store=self.store)
        runtime = CrowdarrrRuntime(
            settings=settings,
            store=self.store,
            qbit=qbit,
            repair_service=repair,
            crowdnfo=crowdnfo,
            library_connectors=library_connectors,
            sab_webhook=sab_webhook,
            health_connectors=health,
            action_queue=queue,
        )
        queue.bind(runtime)
        return _RuntimeBundle(runtime, queue, health, tuple(closeables))

    async def reload_runtime(self) -> None:
        async with self._reload_lock:
            settings = await self.settings.get()
            replacement = self._compose_bundle(settings)
            previous = self.bundle
            self.bundle = replacement
            if previous is not None:
                await previous.close()
            self._configure_backfill(settings)

    def _configure_backfill(self, settings: AppSettings) -> None:
        if self._scheduler is None:
            return
        if settings.download_mode is DownloadMode.NEW_AND_BACKFILL:
            self._scheduler.configure_backfill(settings.backfill_cron)
        else:
            self._scheduler.disable_backfill()

    async def _scheduled_backfill(self) -> None:
        runtime = self.runtime
        if runtime is None:
            return
        try:
            await runtime.full_backfill()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.exception("scheduled backfill failed (%s)", sanitized_error(error))

    async def initialize(self) -> None:
        await self.settings.initialize()
        await self.operations.initialize()
        timezone = os.getenv("TZ", "UTC")
        self._scheduler_backend = AsyncIOScheduler()
        self._scheduler = CrowdarrrScheduler(
            scheduler=self._scheduler_backend,
            backfill_callback=self._scheduled_backfill,
            timezone=timezone,
        )
        await self.reload_runtime()
        self._scheduler_backend.start()

    async def close(self) -> None:
        if self._scheduler_backend is not None:
            self._scheduler_backend.shutdown(wait=False)
        if self.bundle is not None:
            await self.bundle.close()
            self.bundle = None
        await self.settings.close()
        await self.operations.close()

    async def handle_sab_completion(self, event: SABCompletionEvent) -> WorkflowOutcome:
        runtime = self.runtime
        if runtime is None:
            raise RuntimeError("runtime is not initialized")
        return await runtime.handle_sab_completion(event)


def _default_services() -> _DefaultServices:
    config_directory = Path(
        os.getenv(
            "CROWDARRR_CONFIG_DIR",
            os.getenv("CROWDARRR_DATA_DIR", "/config"),
        )
    )
    database = config_directory / "crowdarrr.sqlite3"
    master_key = os.getenv("CROWDARRR_MASTER_KEY") or None
    settings = SettingsStore(database, master_key=master_key)
    operations = OperationsStore(database)
    return _DefaultServices(settings=settings, operations=operations)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def create_app(*, services: Any | None = None, api_token: str | None = None) -> FastAPI:
    """Create an app around injectable services for runtime and isolated tests."""

    if services is None:
        owned_services: _DefaultServices | None = _default_services()
        service_container: Any = owned_services
    else:
        owned_services = None
        service_container = services

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if owned_services is not None:
            await owned_services.initialize()
        try:
            yield
        finally:
            if owned_services is not None:
                await owned_services.close()

    application = FastAPI(
        title="Crowdarrr",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    effective_token = (
        api_token if api_token is not None else os.getenv("CROWDARRR_API_TOKEN")
    )
    application.add_middleware(SecurityMiddleware, api_token=effective_token)

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/dashboard")
    async def dashboard() -> Any:
        return await service_container.dashboard.snapshot()

    @application.get("/api/settings")
    async def get_settings() -> Any:
        return await service_container.settings.public_view()

    @application.put("/api/settings")
    async def update_settings(patch: dict[str, Any]) -> Any:
        try:
            updated = await service_container.settings.update_public(patch)
        except (ValidationError, ValueError):
            return JSONResponse({"detail": "invalid settings"}, status_code=422)
        reload_runtime = getattr(service_container, "reload_runtime", None)
        if callable(reload_runtime):
            pending = reload_runtime()
            if inspect.isawaitable(pending):
                await pending
        return updated

    @application.post("/api/actions/scan-repair", status_code=202)
    async def scan_repair() -> dict[str, str]:
        job_id = await service_container.actions.scan_repair()
        return {"job_id": str(job_id), "status": "accepted"}

    @application.post("/api/torrents/{torrent_hash}/repair", status_code=202)
    async def repair_torrent(torrent_hash: str) -> dict[str, str]:
        job_id = await service_container.actions.repair_torrent(torrent_hash)
        return {"job_id": str(job_id), "status": "accepted"}

    @application.post("/api/actions/misses/{miss_id}/retry", status_code=202)
    async def retry_miss(miss_id: str) -> dict[str, str]:
        job_id = await service_container.actions.retry_miss(miss_id)
        return {"job_id": str(job_id), "status": "accepted"}

    @application.post("/api/webhooks/sabnzbd")
    async def sab_completion(payload: dict[str, Any]) -> Any:
        handler = getattr(service_container, "handle_sab_completion", None)
        if not callable(handler):
            return JSONResponse(
                {"detail": "SAB completion handling is unavailable"},
                status_code=503,
            )
        try:
            event = SABCompletionEvent(
                release_name=str(payload.get("release_name", payload.get("name", ""))),
                storage_path=str(
                    payload.get("storage_path", payload.get("storage", ""))
                ),
                category=str(payload.get("category", "")),
                nzo_id=(
                    str(payload["nzo_id"])
                    if payload.get("nzo_id") is not None
                    else None
                ),
            )
        except ValueError:
            return JSONResponse(
                {"detail": "invalid SAB completion payload"},
                status_code=422,
            )
        result = handler(event)
        return _jsonable(await result if inspect.isawaitable(result) else result)

    @application.post("/api/connectors/{connector}/test")
    async def test_connector(connector: str) -> Any:
        if connector not in _CONNECTOR_NAMES:
            return JSONResponse({"detail": "connector not found"}, status_code=404)
        try:
            result = await service_container.connectors.test(connector)
        except Exception as error:
            detail = sanitized_error(error)
            LOGGER.warning("connector test failed: %s (%s)", connector, detail)
            return JSONResponse(
                {
                    "connector": connector,
                    "status": "unavailable",
                    "detail": detail,
                },
                status_code=503,
            )
        return _jsonable(result)

    @application.get("/api/logs")
    async def logs(limit: int = 200) -> Any:
        safe_limit = min(max(limit, 1), 1_000)
        logs_service = getattr(service_container, "logs", None)
        if logs_service is None:
            return {"items": [], "next_cursor": None}
        result = logs_service.list(limit=safe_limit)
        return await result if inspect.isawaitable(result) else result

    frontend_override = os.getenv("CROWDARRR_FRONTEND_DIR")
    frontend_directory = (
        Path(frontend_override)
        if frontend_override
        else Path(__file__).resolve().parents[1] / "frontend" / "dist"
    )
    frontend_index = frontend_directory / "index.html"
    if frontend_index.is_file():

        @application.get("/")
        async def spa_index() -> FileResponse:
            return FileResponse(frontend_index)

        @application.get("/{requested_path:path}")
        async def spa_fallback(requested_path: str) -> Response:
            if requested_path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            candidate = (frontend_directory / requested_path).resolve()
            try:
                candidate.relative_to(frontend_directory.resolve())
            except ValueError:
                return JSONResponse({"detail": "not found"}, status_code=404)
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend_index)

    return application


app = create_app()
