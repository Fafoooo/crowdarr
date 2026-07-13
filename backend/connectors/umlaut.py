"""UmlautAdaptarr original-title recovery connector."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

import httpx

from backend.connectors.health import (
    ConnectorHealth,
    normalize_base_url,
    sanitized_error,
)
from backend.core.library import sanitize_release_name


class UmlautAdaptarrConnector:
    """Recover original scene names from UmlautAdaptarr's title cache."""

    def __init__(
        self,
        *,
        base_url: str,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = normalize_base_url(base_url, service="UmlautAdaptarr")
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None

    async def recover_release_name(self, changed_title: str) -> str | None:
        safe_title = sanitize_release_name(changed_title)
        if safe_title is None:
            raise ValueError("changed title is unsafe")
        response = await self._http.get(
            f"{self._base_url}/titlelookup",
            params={"changedTitle": safe_title},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = cast(object, response.json())
        if not isinstance(payload, Mapping):
            raise ValueError("UmlautAdaptarr response must be an object")
        return sanitize_release_name(payload.get("originalTitle"))

    async def healthcheck(self) -> ConnectorHealth:
        try:
            response = await self._http.get(
                f"{self._base_url}/titlelookup",
                params={"changedTitle": "crowdarrr-healthcheck"},
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()
            return ConnectorHealth(healthy=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return ConnectorHealth(False, detail=sanitized_error(error))

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> UmlautAdaptarrConnector:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()
