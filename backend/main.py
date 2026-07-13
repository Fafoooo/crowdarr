"""Crowdarrr FastAPI application and single-container SPA entry point."""

from __future__ import annotations

import hmac
import inspect
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from backend.connectors.health import sanitized_error
from backend.db.operations import OperationsStore
from backend.db.settings import SettingsStore

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


class _DefaultDashboard:
    def __init__(self, operations: OperationsStore) -> None:
        self._operations = operations

    async def snapshot(self) -> dict[str, Any]:
        counters = await self._operations.get_counters()
        complete_counters = {name: counters.get(name, 0) for name in _COUNTER_NAMES}
        activity = await self._operations.list_activity(limit=50)
        return {
            "connector_health": {},
            "counters": complete_counters,
            "recent_activity": [
                {
                    "id": str(item.id),
                    "type": item.event_type,
                    "title": item.event_type.replace("_", " ").title(),
                    "message": item.message,
                    "status": str(item.details.get("status", "info")),
                    "created_at": item.created_at.isoformat(),
                    **(
                        {"miss_id": str(item.details["miss_id"])}
                        if "miss_id" in item.details
                        else {}
                    ),
                }
                for item in activity
            ],
            "stuck_torrents": [],
        }


class _DefaultActions:
    """Durably enqueue UI actions for the runtime worker integration."""

    def __init__(self, operations: OperationsStore) -> None:
        self._operations = operations

    async def _enqueue(self, *, prefix: str, kind: str) -> str:
        job_id = f"{prefix}-{uuid4().hex}"
        await self._operations.create_job(job_id=job_id, kind=kind)
        await self._operations.record_activity(
            event_type="job_queued",
            message=f"{kind.replace('_', ' ')} queued",
            details={"job_id": job_id, "status": "info"},
        )
        return job_id

    async def scan_repair(self) -> str:
        return await self._enqueue(prefix="scan", kind="scan_repair")

    async def repair_torrent(self, torrent_hash: str) -> str:
        if not torrent_hash:
            raise ValueError("torrent hash cannot be blank")
        return await self._enqueue(prefix="repair", kind="repair_torrent")

    async def retry_miss(self, miss_id: str) -> str:
        if not miss_id:
            raise ValueError("miss id cannot be blank")
        return await self._enqueue(prefix="retry", kind="retry_miss")


class _DefaultConnectors:
    async def test(self, connector: str) -> dict[str, Any]:
        return {
            "connector": connector,
            "status": "disabled",
            "latency_ms": None,
            "message": "connector runtime is not configured",
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


@dataclass(slots=True)
class _DefaultServices:
    settings: SettingsStore
    dashboard: _DefaultDashboard
    actions: _DefaultActions
    connectors: _DefaultConnectors
    logs: _DefaultLogs
    operations: OperationsStore

    async def initialize(self) -> None:
        await self.settings.initialize()
        await self.operations.initialize()

    async def close(self) -> None:
        await self.settings.close()
        await self.operations.close()


def _default_services() -> _DefaultServices:
    data_directory = Path(os.getenv("CROWDARRR_DATA_DIR", "data"))
    database = data_directory / "crowdarrr.sqlite3"
    master_key = os.getenv("CROWDARRR_MASTER_KEY") or None
    settings = SettingsStore(database, master_key=master_key)
    operations = OperationsStore(database)
    return _DefaultServices(
        settings=settings,
        dashboard=_DefaultDashboard(operations),
        actions=_DefaultActions(operations),
        connectors=_DefaultConnectors(),
        logs=_DefaultLogs(operations),
        operations=operations,
    )


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
            return await service_container.settings.update_public(patch)
        except (ValidationError, ValueError):
            return JSONResponse({"detail": "invalid settings"}, status_code=422)

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
