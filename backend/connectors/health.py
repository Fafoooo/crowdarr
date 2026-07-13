"""Shared connector health and graceful-degradation helpers."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import httpx

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """A connector health result safe to expose through the application API."""

    healthy: bool
    version: str | None = None
    detail: str | None = None
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class ConnectorOperationResult:
    """Outcome of one independently supervised connector operation."""

    value: object | None = None
    skipped: bool = False
    error: str | None = None


def secret_value(value: str | Any | None) -> str | None:
    """Extract a string from either a plain value or Pydantic ``SecretStr``."""

    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter())
    return str(value)


def normalize_base_url(value: str, *, service: str) -> str:
    """Validate and normalize an HTTP base URL without retaining credentials."""

    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as error:
        raise ValueError(f"{service} base_url must be a valid URL") from error
    if url.scheme not in {"http", "https"} or not url.host:
        raise ValueError(f"{service} base_url must be an absolute HTTP(S) URL")
    if url.userinfo:
        raise ValueError(f"{service} base_url must not contain credentials")
    if url.query or url.fragment:
        raise ValueError(f"{service} base_url must not contain query or fragment")
    path = url.path.rstrip("/")
    normalized = url.copy_with(path=path or "/", query=None, fragment=None)
    return str(normalized).rstrip("/")


def sanitized_error(error: BaseException) -> str:
    """Classify failures without leaking URLs, credentials, or response bodies."""

    if isinstance(
        error,
        (
            ConnectionError,
            TimeoutError,
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ),
    ):
        return "connection failed"
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in {401, 403}:
            return "authentication failed"
        if status == 429:
            return "rate limited"
        if status >= 500:
            return "service unavailable"
        return "request failed"
    if isinstance(error, PermissionError):
        return "permission denied"
    if isinstance(error, FileNotFoundError):
        return "file unavailable"
    if isinstance(error, ValueError):
        return "invalid input"
    return "operation failed"


def response_records(payload: object, *, service: str) -> list[Mapping[str, Any]]:
    """Normalize list and paginated-list connector responses."""

    if isinstance(payload, Mapping):
        payload = payload.get("records")
    if not isinstance(payload, list):
        raise ValueError(f"{service} response must contain a list")
    return [entry for entry in payload if isinstance(entry, Mapping)]


def _safe_log_label(value: str) -> str:
    cleaned = "".join(character for character in value if character.isprintable())
    return cleaned[:80] or "connector"


class ConnectorSupervisor:
    """Run optional connectors independently so one outage cannot abort a scan."""

    async def run_all(
        self,
        *,
        operation: str,
        connectors: Mapping[str, object],
    ) -> dict[str, ConnectorOperationResult]:
        if not operation.isidentifier() or operation.startswith("_"):
            raise ValueError("operation must name a public connector method")

        async def run_one(
            name: str, connector: object
        ) -> tuple[str, ConnectorOperationResult]:
            label = _safe_log_label(name)
            method = getattr(connector, operation, None)
            if not callable(method):
                LOGGER.info("%s unavailable during %s (unsupported)", label, operation)
                return name, ConnectorOperationResult(
                    skipped=True,
                    error="operation unsupported",
                )
            try:
                pending = method()
                if not inspect.isawaitable(pending):
                    raise TypeError("connector operation must be asynchronous")
                value = await cast(Awaitable[object], pending)
                return name, ConnectorOperationResult(value=value)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                safe_error = sanitized_error(error)
                LOGGER.info(
                    "%s unavailable during %s (%s)",
                    label,
                    operation,
                    safe_error,
                )
                return name, ConnectorOperationResult(
                    skipped=True,
                    error=safe_error,
                )

        outcomes = await asyncio.gather(
            *(run_one(name, connector) for name, connector in connectors.items())
        )
        return dict(outcomes)
