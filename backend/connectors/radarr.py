"""Async Radarr v3 library connector."""

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


def _remote_media_path(
    item: Mapping[str, Any], file_data: Mapping[str, Any]
) -> str | None:
    direct = file_data.get("path")
    if isinstance(direct, str) and direct:
        return direct
    root = item.get("path")
    relative = file_data.get("relativePath")
    if isinstance(root, str) and isinstance(relative, str) and root and relative:
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return None
        return str(PurePosixPath(root).joinpath(relative_path))
    return None


class RadarrConnector:
    """Enumerate Radarr movie files using scene names when available."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | Any,
        http_client: httpx.AsyncClient | None = None,
        path_mapper: PathMapper,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = normalize_base_url(base_url, service="Radarr")
        self._api_key = secret_value(api_key)
        if not self._api_key:
            raise ValueError("Radarr api_key is required")
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None
        self._path_mapper = path_mapper

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": cast(str, self._api_key)}

    async def list_media(self) -> list[LibraryMediaItem]:
        response = await self._http.get(
            f"{self._base_url}/api/v3/movie",
            headers=self._headers,
        )
        response.raise_for_status()
        movies = response_records(cast(object, response.json()), service="Radarr")
        results: list[LibraryMediaItem] = []
        for movie in movies:
            if movie.get("hasFile") is False:
                continue
            file_data = movie.get("movieFile")
            if not isinstance(file_data, Mapping):
                continue
            remote_path = _remote_media_path(movie, file_data)
            if remote_path is None:
                continue
            release_name = sanitize_release_name(file_data.get("sceneName"))
            if release_name is None:
                release_name = sanitize_release_name(PurePosixPath(remote_path).stem)
            if release_name is None:
                LOGGER.warning("Skipping Radarr item with unsafe release name")
                continue
            try:
                local_path = self._path_mapper.map_path(remote_path)
            except (OSError, ValueError):
                LOGGER.warning("Skipping Radarr item with unsafe mapped path")
                continue
            item_id = file_data.get("id", movie.get("id"))
            if not isinstance(item_id, (int, str)):
                item_id = None
            results.append(
                LibraryMediaItem(
                    release_name=release_name,
                    local_media_path=local_path,
                    source="radarr",
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

    async def __aenter__(self) -> RadarrConnector:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()
