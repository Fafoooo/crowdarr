"""Async CrowdNFO client with byte-preserving downloads."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from backend.crowdnfo.endpoints import DEFAULT_CONTRACT, CrowdNFOContract


class UnsupportedLookupError(RuntimeError):
    """Raised when the live CrowdNFO API cannot perform a requested lookup."""


@dataclass(frozen=True, slots=True)
class CrowdNFOFileMetadata:
    """Metadata returned by the best-file endpoint."""

    file_id: str
    original_file_name: str | None = None
    file_size_bytes: int | None = None
    file_type: str | None = None
    raw: Mapping[str, Any] | None = None


class CrowdNFOClient:
    """Small testable client for the verified CrowdNFO API surface.

    The caller may inject an ``httpx.AsyncClient``. Injected clients remain owned
    by the caller; an internally created client can be closed with ``aclose``.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://crowdnfo.net",
        api_key: str | Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        contract: CrowdNFOContract = DEFAULT_CONTRACT,
        timeout: float = 30.0,
        max_concurrency: int = 4,
        max_retries: int = 2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._base_url = self._normalize_base_url(base_url)
        self._api_key = self._secret_value(api_key)
        self._contract = contract
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None
        self._limit = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._sleep = sleep

    @staticmethod
    def _secret_value(value: str | Any | None) -> str | None:
        if value is None:
            return None
        getter = getattr(value, "get_secret_value", None)
        if callable(getter):
            return str(getter())
        return str(value)

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("CrowdNFO base_url must be an absolute HTTP(S) URL")
        path = url.path.rstrip("/")
        if path == "/api":
            path = ""
        elif path:
            raise ValueError("CrowdNFO base_url must not contain a path")
        return str(url.copy_with(path=path or "/", query=None, fragment=None)).rstrip(
            "/"
        )

    @staticmethod
    def _segment(value: str) -> str:
        if not value:
            raise ValueError("path identifiers cannot be blank")
        return quote(value, safe="")

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {self._contract.api_key_header: self._api_key}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self._headers())
        attempts = self._max_retries + 1 if method == "GET" else 1
        async with self._limit:
            for attempt in range(attempts):
                response = await self._http.request(
                    method,
                    self._url(path),
                    headers=headers,
                    **kwargs,
                )
                if response.status_code != 429 and response.status_code < 500:
                    response.raise_for_status()
                    return response
                if attempt + 1 == attempts:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 0.25 * (2**attempt)
                await self._sleep(delay)
        raise RuntimeError("unreachable CrowdNFO request state")

    async def lookup(
        self,
        *,
        media_sha256: str | None = None,
        release_name: str | None = None,
    ) -> CrowdNFOFileMetadata:
        """Look up the best NFO metadata for an exact release name.

        The current API exposes hashes on release details but has no documented
        hash-only query. A hash-only call therefore fails locally and performs no
        request, allowing the matching layer to use its release-name fallback.
        """

        if release_name is None:
            if media_sha256 is not None:
                raise UnsupportedLookupError(
                    "hash-only CrowdNFO lookup is not available in the current API"
                )
            raise ValueError("release_name is required")

        path = self._contract.best_file_path.format(
            release_name=self._segment(release_name)
        )
        response = await self._request(
            "GET",
            path,
            params={"type": "NFO", "raw": "false", "fallback": "false"},
        )
        data = response.json()
        if not isinstance(data, Mapping) or not data.get("fileId"):
            raise ValueError("CrowdNFO best-file response did not contain fileId")
        size = data.get("fileSizeBytes")
        return CrowdNFOFileMetadata(
            file_id=str(data["fileId"]),
            original_file_name=(
                str(data["originalFileName"])
                if data.get("originalFileName") is not None
                else None
            ),
            file_size_bytes=int(size) if size is not None else None,
            file_type=str(data["fileType"]) if data.get("fileType") else None,
            raw=data,
        )

    async def download_nfo(
        self,
        *,
        release_name: str | None = None,
        file_id: str | None = None,
        media_sha256: str | None = None,
    ) -> bytes:
        """Return the NFO response body exactly as received, without decoding."""

        if file_id is None:
            metadata = await self.lookup(
                media_sha256=media_sha256,
                release_name=release_name,
            )
            file_id = metadata.file_id
        path = self._contract.file_download_path.format(file_id=self._segment(file_id))
        response = await self._request("GET", path)
        return response.content

    async def _upload_file(
        self,
        *,
        release_name: str,
        filename: str,
        content: bytes,
        file_type: str,
        media_sha256: str | None,
        category: str,
    ) -> Mapping[str, Any] | None:
        path = self._contract.file_upload_path.format(
            release_name=self._segment(release_name)
        )
        data = {
            "FileType": file_type,
            "OriginalFileName": filename,
            "Category": category,
        }
        if media_sha256:
            data["FileHash"] = media_sha256
        response = await self._request(
            "POST",
            path,
            data=data,
            files={"File": (filename, content, "application/octet-stream")},
        )
        if not response.content:
            return None
        parsed = response.json()
        return parsed if isinstance(parsed, Mapping) else None

    async def upload_nfo(
        self,
        *,
        release_name: str,
        filename: str,
        content: bytes,
        media_sha256: str | None = None,
        category: str = "Unknown",
    ) -> Mapping[str, Any] | None:
        return await self._upload_file(
            release_name=release_name,
            filename=filename,
            content=content,
            file_type="NFO",
            media_sha256=media_sha256,
            category=category,
        )

    async def upload_mediainfo(
        self,
        *,
        release_name: str,
        filename: str,
        content: bytes,
        media_sha256: str | None = None,
        category: str = "Unknown",
    ) -> Mapping[str, Any] | None:
        return await self._upload_file(
            release_name=release_name,
            filename=filename,
            content=content,
            file_type="MediaInfo",
            media_sha256=media_sha256,
            category=category,
        )

    async def upload_filelist(
        self,
        *,
        release_name: str,
        entries: Sequence[Mapping[str, Any]],
        media_sha256: str | None = None,
        category: str = "Unknown",
    ) -> Mapping[str, Any] | None:
        path = self._contract.filelist_upload_path.format(
            release_name=self._segment(release_name)
        )
        normalized_entries = [
            {
                "filePath": entry.get("file_path", entry.get("filePath")),
                "fileSizeBytes": entry.get(
                    "file_size_bytes", entry.get("fileSizeBytes")
                ),
            }
            for entry in entries
        ]
        body: dict[str, Any] = {
            "releaseName": release_name,
            "category": category,
            "entries": normalized_entries,
        }
        if media_sha256:
            body["fileHash"] = media_sha256
        response = await self._request("POST", path, json=body)
        if not response.content:
            return None
        parsed = response.json()
        return parsed if isinstance(parsed, Mapping) else None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> CrowdNFOClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()
