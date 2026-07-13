"""Async SABnzbd history and completion-webhook integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import httpx

from backend.connectors.health import (
    ConnectorHealth,
    normalize_base_url,
    sanitized_error,
    secret_value,
)
from backend.core.library import sanitize_release_name

LOGGER = logging.getLogger(__name__)


def _safe_optional_text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > limit:
        return None
    if any(not character.isprintable() for character in candidate):
        return None
    return candidate


@dataclass(frozen=True, slots=True)
class SABCompletionEvent:
    """A completed SABnzbd job suitable for live-in/live-out processing."""

    release_name: str
    storage_path: str
    category: str = ""
    nzo_id: str | None = None

    def __post_init__(self) -> None:
        release_name = sanitize_release_name(self.release_name)
        storage_path = _safe_optional_text(self.storage_path, limit=4096)
        category = _safe_optional_text(self.category, limit=100) or ""
        nzo_id = _safe_optional_text(self.nzo_id, limit=200)
        if release_name is None:
            raise ValueError("unsafe SABnzbd release name")
        if storage_path is None:
            raise ValueError("unsafe SABnzbd storage path")
        object.__setattr__(self, "release_name", release_name)
        object.__setattr__(self, "storage_path", storage_path)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "nzo_id", nzo_id)

    @property
    def remote_storage_path(self) -> str:
        return self.storage_path


class SABLiveService(Protocol):
    async def fetch_missing(self, event: SABCompletionEvent) -> object: ...

    async def contribute(self, event: SABCompletionEvent) -> object: ...


@dataclass(frozen=True, slots=True)
class SABLiveActionResult:
    """Describe whether a live action changed state and may be finalized."""

    performed: bool
    terminal: bool = True
    value: object | None = None
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class SABWebhookResult:
    accepted: bool
    actions: tuple[str, ...] = ()
    errors: Mapping[str, str] = field(default_factory=dict)
    # None preserves the pre-v0.1.3 contract: successful attempted actions count.
    performed_actions: tuple[str, ...] | None = None
    deferred_actions: tuple[str, ...] = ()
    warnings: Mapping[str, str] = field(default_factory=dict)


class SABnzbdConnector:
    """Small async client for the SABnzbd JSON API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | Any | None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = normalize_base_url(base_url, service="SABnzbd")
        self._api_key = secret_value(api_key)
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None

    def _params(self, *, mode: str) -> dict[str, str]:
        params = {"mode": mode, "output": "json"}
        if self._api_key:
            params["apikey"] = self._api_key
        return params

    async def list_completed(self) -> list[SABCompletionEvent]:
        response = await self._http.get(
            f"{self._base_url}/api",
            params=self._params(mode="history"),
        )
        response.raise_for_status()
        payload = cast(object, response.json())
        if not isinstance(payload, Mapping):
            raise ValueError("SABnzbd history response must be an object")
        history = payload.get("history")
        if not isinstance(history, Mapping):
            raise ValueError("SABnzbd history response is missing history")
        slots = history.get("slots", [])
        if not isinstance(slots, list):
            raise ValueError("SABnzbd history slots must be a list")

        completed: list[SABCompletionEvent] = []
        for raw_slot in slots:
            if not isinstance(raw_slot, Mapping):
                continue
            status = raw_slot.get("status")
            if not isinstance(status, str) or status.casefold() != "completed":
                continue
            release_name = sanitize_release_name(raw_slot.get("name"))
            storage = _safe_optional_text(raw_slot.get("storage"), limit=4096)
            if release_name is None or storage is None:
                LOGGER.warning("Skipping unsafe or incomplete SABnzbd history entry")
                continue
            completed.append(
                SABCompletionEvent(
                    release_name=release_name,
                    storage_path=storage,
                    category=_safe_optional_text(raw_slot.get("category"), limit=100)
                    or "",
                    nzo_id=_safe_optional_text(raw_slot.get("nzo_id"), limit=200),
                )
            )
        return completed

    async def scan(self) -> list[SABCompletionEvent]:
        """Expose history enumeration through the common supervisor operation."""

        return await self.list_completed()

    async def healthcheck(self) -> ConnectorHealth:
        try:
            response = await self._http.get(
                f"{self._base_url}/api",
                params=self._params(mode="version"),
            )
            response.raise_for_status()
            version: str | None = None
            try:
                payload = cast(object, response.json())
            except ValueError:
                payload = None
            if isinstance(payload, Mapping):
                version = _safe_optional_text(payload.get("version"), limit=100)
            return ConnectorHealth(healthy=True, version=version)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return ConnectorHealth(
                healthy=False,
                detail=sanitized_error(error),
            )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> SABnzbdConnector:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()


class SABWebhookHandler:
    """Dispatch one completion event to independently toggled live services."""

    def __init__(
        self,
        *,
        live_service: SABLiveService,
        fetch_enabled: bool,
        contribute_enabled: bool,
    ) -> None:
        self._live_service = live_service
        self._fetch_enabled = fetch_enabled
        self._contribute_enabled = contribute_enabled

    async def handle(self, event: SABCompletionEvent) -> SABWebhookResult:
        actions: list[str] = []
        performed_actions: list[str] = []
        deferred_actions: list[str] = []
        errors: dict[str, str] = {}
        warnings: dict[str, str] = {}
        operations = (
            ("fetch", self._fetch_enabled, self._live_service.fetch_missing),
            ("contribute", self._contribute_enabled, self._live_service.contribute),
        )
        for name, enabled, operation in operations:
            if not enabled:
                continue
            actions.append(name)
            try:
                operation_result = await operation(event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                safe_error = sanitized_error(error)
                errors[name] = safe_error
                LOGGER.warning("SABnzbd %s step failed (%s)", name, safe_error)
            else:
                if isinstance(operation_result, SABLiveActionResult):
                    if operation_result.performed:
                        performed_actions.append(name)
                    if not operation_result.terminal:
                        deferred_actions.append(name)
                    if operation_result.warning:
                        warnings[name] = operation_result.warning
                else:
                    # Third-party/legacy services only signalled failure by raising.
                    performed_actions.append(name)
        return SABWebhookResult(
            accepted=True,
            actions=tuple(actions),
            errors=errors,
            performed_actions=tuple(performed_actions),
            deferred_actions=tuple(deferred_actions),
            warnings=warnings,
        )
