"""Crowdarrr FastAPI application and single-container SPA entry point."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any, cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from backend.connectors.health import ConnectorHealth, sanitized_error
from backend.connectors.qbit import QBitConnector
from backend.connectors.radarr import RadarrConnector
from backend.connectors.sab import (
    SABCompletionEvent,
    SABnzbdConnector,
    SABWebhookHandler,
)
from backend.connectors.sonarr import SonarrConnector
from backend.connectors.umlaut import UmlautAdaptarrConnector
from backend.core.contribution import (
    ContributionService,
    CrowdNFOUploader,
)
from backend.core.files import MismatchCleanupPolicy, PathMapper, PathMapping
from backend.core.library import LibraryMediaItem
from backend.core.mediainfo import MediaInfoRunner
from backend.core.repair import TorrentRepairService
from backend.core.scan import ScanTrigger, mode_allows_trigger
from backend.core.scheduler import CrowdarrrScheduler
from backend.core.settings import AppSettings, DownloadMode, SettingsPatch
from backend.crowdnfo.client import CrowdNFOClient
from backend.db.operations import OperationsStore
from backend.db.settings import SettingsEncryptionError, SettingsStore
from backend.runtime import (
    ActionQueueFull,
    AsyncHashService,
    CrowdarrrRuntime,
    HealthConnector,
    InProcessActionQueue,
    OperationsRuntimeStore,
    QBitCompletedPoller,
    QBitLiveWorkflow,
    SABLiveWorkflow,
    StrategyAwareCrowdNFODownloader,
    WorkflowOutcome,
)

LOGGER = logging.getLogger(__name__)

_CONNECTOR_NAMES = frozenset(
    {"crowdnfo", "qbittorrent", "sabnzbd", "radarr", "sonarr", "umlautadaptarr"}
)
_COUNTER_NAMES = ("fetched", "repaired", "uploaded", "matches", "misses")
_SAB_WEBHOOK_PATH = "/api/webhooks/sabnzbd"
_SAB_WEBHOOK_SECRET_HEADER = "X-Crowdarrr-SAB-Secret"
_DEFAULT_SAB_WEBHOOK_MAX_BYTES = 64 * 1024


def _positive_int(value: str | None, *, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        LOGGER.warning("Ignoring invalid %s", name)
        return default
    if parsed < 1:
        LOGGER.warning("Ignoring non-positive %s", name)
        return default
    return parsed


class SecurityMiddleware(BaseHTTPMiddleware):
    """Apply API bearer protection and browser hardening headers."""

    def __init__(
        self,
        app: Any,
        *,
        api_token: str | None,
        api_token_provider: Callable[[], Awaitable[str | None]] | None,
        sab_webhook_secret: str | None,
        sab_webhook_max_bytes: int,
    ) -> None:
        super().__init__(app)
        self._api_token = api_token or None
        self._api_token_provider = api_token_provider
        self._sab_webhook_secret = sab_webhook_secret or None
        self._sab_webhook_max_bytes = sab_webhook_max_bytes

    @staticmethod
    def _authorized(request: Request, api_token: str) -> bool:
        authorization = request.headers.get("Authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        return bool(
            separator
            and scheme.casefold() == "bearer"
            and credential
            and hmac.compare_digest(credential, api_token)
        )

    def _sab_authorized(self, request: Request) -> bool:
        credential = request.headers.get(_SAB_WEBHOOK_SECRET_HEADER, "")
        return bool(
            credential
            and self._sab_webhook_secret is not None
            and hmac.compare_digest(credential, self._sab_webhook_secret)
        )

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        is_sab_webhook = request.url.path == _SAB_WEBHOOK_PATH
        protected = request.url.path.startswith("/api") and request.url.path != (
            "/api/health"
        )
        api_token = self._api_token
        token_provider_failed = False
        if (
            protected
            and not is_sab_webhook
            and api_token is None
            and self._api_token_provider is not None
        ):
            try:
                api_token = await self._api_token_provider()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                token_provider_failed = True
                LOGGER.error(
                    "application API token could not be loaded (%s)",
                    sanitized_error(error),
                )
        if is_sab_webhook and self._sab_webhook_secret is None:
            response: Response = JSONResponse(
                {"detail": "SAB webhook secret is not configured"},
                status_code=503,
            )
        elif is_sab_webhook and not self._sab_authorized(request):
            response = JSONResponse(
                {"detail": "authentication required"}, status_code=401
            )
        elif is_sab_webhook:
            raw_length = request.headers.get("Content-Length")
            try:
                content_length = int(raw_length) if raw_length is not None else None
            except ValueError:
                content_length = self._sab_webhook_max_bytes + 1
            if (
                content_length is not None
                and content_length > self._sab_webhook_max_bytes
            ):
                response = JSONResponse(
                    {"detail": "SAB webhook payload is too large"},
                    status_code=413,
                )
            else:
                body = await request.body()
                if len(body) > self._sab_webhook_max_bytes:
                    response = JSONResponse(
                        {"detail": "SAB webhook payload is too large"},
                        status_code=413,
                    )
                else:
                    response = await call_next(request)
        elif token_provider_failed:
            response = JSONResponse(
                {"detail": "security configuration is unavailable"},
                status_code=503,
            )
        elif (
            api_token is not None
            and protected
            and not self._authorized(request, api_token)
        ):
            response = JSONResponse(
                {"detail": "authentication required"}, status_code=401
            )
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; "
            "object-src 'none'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'"
        )
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=()"
        )
        return response


class _CrowdNFOHealth:
    """Verify that the configured profile API key is accepted."""

    def __init__(self, client: CrowdNFOClient) -> None:
        self._client = client

    async def healthcheck(self) -> ConnectorHealth:
        try:
            await self._client.validate_api_key()
        except asyncio.CancelledError:
            raise
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


@dataclass(slots=True)
class _RuntimeBundle:
    runtime: CrowdarrrRuntime
    queue: InProcessActionQueue
    health_connectors: dict[str, HealthConnector]
    closeables: tuple[object, ...]
    qbit_poller: QBitCompletedPoller | None = None

    def start(self) -> None:
        if self.qbit_poller is not None:
            self.qbit_poller.start()

    async def close(self) -> None:
        if self.qbit_poller is not None:
            await self.qbit_poller.close()
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
                "dry_run": True,
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

    async def job_status(self, job_id: str) -> dict[str, Any]:
        job = await self._services.operations.get_job(job_id)
        return {
            "job_id": job.job_id,
            "kind": job.kind,
            "status": job.status,
            "result": job.result,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }


class _RuntimeConnectors:
    def __init__(self, services: _DefaultServices) -> None:
        self._services = services

    async def test(
        self,
        connector: str,
        patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = await self._services.settings.get()
        temporary_bundle: _RuntimeBundle | None = None
        try:
            if patch is not None:
                settings = SettingsStore.apply_patch(
                    settings,
                    SettingsPatch.model_validate({connector: patch}),
                )
                temporary_bundle = self._services._compose_bundle(settings)
            enabled = (
                bool(settings.crowdnfo.api_key.get_secret_value())
                if connector == "crowdnfo"
                else bool(getattr(getattr(settings, connector, None), "enabled", False))
            )
            if not enabled:
                return {
                    "connector": connector,
                    "status": "disabled",
                    "latency_ms": None,
                    "message": "connector is disabled",
                }
            bundle = temporary_bundle or self._services.bundle
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
        finally:
            if temporary_bundle is not None:
                await temporary_bundle.close()


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
        hash_service = AsyncHashService(
            cache=self.operations,
            max_size_bytes=settings.hash_max_size_bytes,
            max_concurrency=_positive_int(
                os.getenv("CROWDARRR_HASH_MAX_CONCURRENCY"),
                default=2,
                name="CROWDARRR_HASH_MAX_CONCURRENCY",
            ),
        )

        crowdnfo: CrowdNFOClient | None = None
        crowdnfo_key = settings.crowdnfo.api_key.get_secret_value()
        if crowdnfo_key:
            try:
                crowdnfo = CrowdNFOClient(
                    base_url=str(settings.crowdnfo.base_url),
                    api_key=crowdnfo_key,
                )
            except Exception as error:
                LOGGER.warning(
                    "CrowdNFO runtime unavailable (%s)", sanitized_error(error)
                )
        if crowdnfo is not None:
            closeables.append(crowdnfo)
            health["crowdnfo"] = _CrowdNFOHealth(crowdnfo)
        crowdnfo_downloader = (
            StrategyAwareCrowdNFODownloader(
                client=crowdnfo,
                mode=settings.match_strategy,
            )
            if crowdnfo is not None
            else None
        )

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
            if path_mapper is not None:
                library_connectors[name] = (
                    _ReleaseResolvingLibraryConnector(connector, umlaut)
                    if umlaut is not None
                    else connector
                )

        repair = (
            TorrentRepairService(
                crowdnfo=crowdnfo_downloader,
                qbit=qbit,
                path_mapper=path_mapper,
                allowed_roots=[
                    Path(mapping.local_root) for mapping in settings.path_mappings
                ],
                dry_run=settings.dry_run,
                auto_recheck=settings.auto_recheck,
                hash_service=(
                    hash_service
                    if settings.match_strategy != "release_name_only"
                    else None
                ),
                keep_mismatch=(
                    settings.nfo_mismatch_policy is MismatchCleanupPolicy.KEEP
                ),
            )
            if (
                crowdnfo_downloader is not None
                and qbit is not None
                and path_mapper is not None
            )
            else None
        )

        fetch_enabled = mode_allows_trigger(
            settings.download_mode,
            ScanTrigger.NEW_DOWNLOAD,
        )
        contribute_enabled = settings.contribute.enabled
        sab_webhook: SABWebhookHandler | None = None
        qbit_poller: QBitCompletedPoller | None = None
        if (
            crowdnfo is not None
            and crowdnfo_downloader is not None
            and path_mapper is not None
            and (sab is not None or qbit is not None)
            and (fetch_enabled or contribute_enabled)
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
            live_workflow = SABLiveWorkflow(
                settings=settings,
                path_mapper=path_mapper,
                crowdnfo=crowdnfo_downloader,
                contribution=contribution,
                hash_service=hash_service,
            )
            if sab is not None:
                sab_webhook = SABWebhookHandler(
                    live_service=live_workflow,
                    fetch_enabled=fetch_enabled,
                    contribute_enabled=contribute_enabled,
                )
            if qbit is not None:
                qbit_poller = QBitCompletedPoller(
                    qbit=qbit,
                    live_service=QBitLiveWorkflow(live_workflow),
                    store=self.operations,
                    fetch_enabled=fetch_enabled,
                    contribute_enabled=contribute_enabled,
                    poll_interval=float(
                        _positive_int(
                            os.getenv("CROWDARRR_QBIT_POLL_INTERVAL_SECONDS"),
                            default=30,
                            name="CROWDARRR_QBIT_POLL_INTERVAL_SECONDS",
                        )
                    ),
                )

        queue = InProcessActionQueue(
            store=self.store,
            max_concurrency=_positive_int(
                os.getenv("CROWDARRR_ACTION_MAX_CONCURRENCY"),
                default=2,
                name="CROWDARRR_ACTION_MAX_CONCURRENCY",
            ),
            max_pending=_positive_int(
                os.getenv("CROWDARRR_ACTION_MAX_PENDING"),
                default=64,
                name="CROWDARRR_ACTION_MAX_PENDING",
            ),
        )
        runtime = CrowdarrrRuntime(
            settings=settings,
            store=self.store,
            qbit=qbit,
            repair_service=repair,
            crowdnfo=crowdnfo_downloader,
            library_connectors=library_connectors,
            sab_webhook=sab_webhook,
            sab_history=sab if sab_webhook is not None else None,
            health_connectors=health,
            action_queue=queue,
            hash_service=hash_service,
            healthcheck_timeout=float(
                _positive_int(
                    os.getenv("CROWDARRR_HEALTHCHECK_TIMEOUT_SECONDS"),
                    default=5,
                    name="CROWDARRR_HEALTHCHECK_TIMEOUT_SECONDS",
                )
            ),
        )
        queue.bind(runtime)
        return _RuntimeBundle(
            runtime,
            queue,
            health,
            tuple(closeables),
            qbit_poller,
        )

    async def reload_runtime(self) -> None:
        async with self._reload_lock:
            settings = await self.settings.get()
            replacement = self._compose_bundle(settings)
            previous = self.bundle
            self.bundle = replacement
            if previous is not None:
                await previous.close()
            replacement.start()
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
            await runtime.enqueue_scheduled_backfill()
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


def create_app(
    *,
    services: Any | None = None,
    api_token: str | None = None,
    sab_webhook_secret: str | None = None,
) -> FastAPI:
    """Create an app around injectable services for runtime and isolated tests."""

    if services is None:
        owned_services: _DefaultServices | None = _default_services()
        service_container: Any = owned_services
    else:
        owned_services = None
        service_container = services

    async def stored_api_token() -> str | None:
        settings_service = getattr(service_container, "settings", None)
        getter = getattr(settings_service, "get", None)
        if not callable(getter):
            return None
        loaded = getter()
        settings = await loaded if inspect.isawaitable(loaded) else loaded
        secret = getattr(settings, "application_api_token", None)
        reveal = getattr(secret, "get_secret_value", None)
        value = reveal() if callable(reveal) else secret
        return str(value) if value else None

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
        version="0.1.2",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    effective_token = (
        api_token if api_token is not None else os.getenv("CROWDARRR_API_TOKEN")
    )
    effective_sab_secret = (
        sab_webhook_secret
        if sab_webhook_secret is not None
        else os.getenv("CROWDARRR_SAB_WEBHOOK_SECRET")
    )
    sab_webhook_max_bytes = _positive_int(
        os.getenv("CROWDARRR_SAB_WEBHOOK_MAX_BYTES"),
        default=_DEFAULT_SAB_WEBHOOK_MAX_BYTES,
        name="CROWDARRR_SAB_WEBHOOK_MAX_BYTES",
    )
    application.add_middleware(
        SecurityMiddleware,
        api_token=effective_token,
        api_token_provider=stored_api_token,
        sab_webhook_secret=effective_sab_secret,
        sab_webhook_max_bytes=sab_webhook_max_bytes,
    )

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
        except SettingsEncryptionError:
            LOGGER.error(
                "settings encryption unavailable; verify CROWDARRR_MASTER_KEY "
                "and the persisted settings key"
            )
            return JSONResponse(
                {
                    "detail": (
                        "settings encryption is unavailable; check "
                        "CROWDARRR_MASTER_KEY and server logs"
                    )
                },
                status_code=503,
            )
        reload_runtime = getattr(service_container, "reload_runtime", None)
        if callable(reload_runtime):
            pending = reload_runtime()
            if inspect.isawaitable(pending):
                await pending
        return updated

    @application.post("/api/actions/scan-repair", status_code=202)
    async def scan_repair() -> Any:
        try:
            job_id = await service_container.actions.scan_repair()
        except ActionQueueFull:
            return JSONResponse(
                {"detail": "action queue is full"},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        return {"job_id": str(job_id), "status": "accepted"}

    @application.get("/api/jobs/{job_id}")
    async def job_status(job_id: str) -> Any:
        try:
            return await service_container.actions.job_status(job_id)
        except KeyError:
            return JSONResponse({"detail": "job not found"}, status_code=404)

    @application.post("/api/torrents/{torrent_hash}/repair", status_code=202)
    async def repair_torrent(torrent_hash: str) -> Any:
        try:
            job_id = await service_container.actions.repair_torrent(torrent_hash)
        except ActionQueueFull:
            return JSONResponse(
                {"detail": "action queue is full"},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        return {"job_id": str(job_id), "status": "accepted"}

    @application.post("/api/actions/misses/{miss_id}/retry", status_code=202)
    async def retry_miss(miss_id: str) -> Any:
        try:
            job_id = await service_container.actions.retry_miss(miss_id)
        except ActionQueueFull:
            return JSONResponse(
                {"detail": "action queue is full"},
                status_code=503,
                headers={"Retry-After": "1"},
            )
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
        resolved = await result if inspect.isawaitable(result) else result
        if isinstance(resolved, WorkflowOutcome) and resolved.status == "failed":
            return JSONResponse(_jsonable(resolved), status_code=422)
        return _jsonable(resolved)

    @application.post("/api/connectors/{connector}/test")
    async def test_connector(
        connector: str,
        patch: dict[str, Any] | None = None,
    ) -> Any:
        if connector not in _CONNECTOR_NAMES:
            return JSONResponse({"detail": "connector not found"}, status_code=404)
        try:
            result = await service_container.connectors.test(connector, patch)
        except ValidationError:
            return JSONResponse(
                {"detail": "invalid connector settings"},
                status_code=422,
            )
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
