"""Async Sonarr v3 library connector."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, cast

import httpx

from backend.connectors.health import (
    ConnectorHealth,
    normalize_base_url,
    response_records,
    sanitized_error,
    secret_value,
)
from backend.core.files import PathMapper
from backend.core.library import LibraryMediaItem, sanitize_release_name

LOGGER = logging.getLogger(__name__)


class SonarrConnector:
    """Enumerate Sonarr episode files while preserving their scene names."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | Any,
        http_client: httpx.AsyncClient | None = None,
        path_mapper: PathMapper | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = normalize_base_url(base_url, service="Sonarr")
        self._api_key = secret_value(api_key)
        if not self._api_key:
            raise ValueError("Sonarr api_key is required")
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None
        self._path_mapper = path_mapper

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": cast(str, self._api_key)}

    async def list_media(self) -> list[LibraryMediaItem]:
        if self._path_mapper is None:
            raise ValueError("Sonarr path mapping is required for library scans")
        response = await self._http.get(
            f"{self._base_url}/api/v3/series",
            headers=self._headers,
        )
        response.raise_for_status()
        series_records = response_records(
            cast(object, response.json()), service="Sonarr"
        )
        results: list[LibraryMediaItem] = []
        for series in series_records:
            series_id = series.get("id")
            if not isinstance(series_id, (int, str)):
                continue
            file_response = await self._http.get(
                f"{self._base_url}/api/v3/episodefile",
                headers=self._headers,
                params={"seriesId": str(series_id)},
            )
            file_response.raise_for_status()
            episode_files = response_records(
                cast(object, file_response.json()), service="Sonarr"
            )
            for file_data in episode_files:
                remote_path = file_data.get("path")
                if not isinstance(remote_path, str) or not remote_path:
                    continue
                release_name = sanitize_release_name(file_data.get("sceneName"))
                if release_name is None:
                    release_name = sanitize_release_name(
                        PurePosixPath(remote_path).stem
                    )
                if release_name is None:
                    LOGGER.warning("Skipping Sonarr item with unsafe release name")
                    continue
                try:
                    local_path = self._path_mapper.map_path(remote_path)
                except (OSError, ValueError):
                    LOGGER.warning("Skipping Sonarr item with unsafe mapped path")
                    continue
                item_id = file_data.get("id")
                if not isinstance(item_id, (int, str)):
                    item_id = None
                results.append(
                    LibraryMediaItem(
                        release_name=release_name,
                        local_media_path=local_path,
                        source="sonarr",
                        remote_media_path=remote_path,
                        item_id=item_id,
                    )
                )
        return results

    async def scan(self) -> list[LibraryMediaItem]:
        """Expose media enumeration through the common supervisor operation."""

        return await self.list_media()

    async def healthcheck(self) -> ConnectorHealth:
        try:
            response = await self._http.get(
                f"{self._base_url}/api/v3/system/status",
                headers=self._headers,
            )
            response.raise_for_status()
            payload = cast(object, response.json())
            version = None
            if isinstance(payload, Mapping) and isinstance(payload.get("version"), str):
                version = payload["version"][:100]
            return ConnectorHealth(healthy=True, version=version)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return ConnectorHealth(False, detail=sanitized_error(error))

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> SonarrConnector:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()
