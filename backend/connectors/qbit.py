"""qBittorrent WebUI adapter and missing-NFO detection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

import httpx

if TYPE_CHECKING:
    from backend.connectors.health import ConnectorHealth
    from backend.core.files import PathMapper


VIDEO_SUFFIXES = frozenset(
    {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
)


@dataclass(frozen=True, slots=True)
class TorrentFile:
    index: int
    path: str
    size: int
    progress: float
    priority: int


@dataclass(frozen=True, slots=True)
class TorrentSnapshot:
    torrent_hash: str
    name: str
    category: str
    content_path: str
    progress: float
    state: str
    save_path: str = ""
    files: list[TorrentFile] = field(default_factory=list)
    local_content_path: Path | None = None


@dataclass(frozen=True, slots=True)
class MissingNFO:
    torrent_hash: str
    torrent_name: str
    file_index: int
    relative_path: PurePosixPath
    reported_path: PurePosixPath


NFOReadiness = Literal[
    "complete",
    "ready",
    "no_incomplete_nfo",
    "no_video",
    "video_incomplete",
    "invalid_nfo_path",
]


@dataclass(frozen=True, slots=True)
class NFORepairAssessment:
    """Explain whether an incomplete torrent is ready for NFO-only repair."""

    status: NFOReadiness
    candidates: tuple[MissingNFO, ...] = ()
    incomplete_nfo_count: int = 0
    reported_nfo_paths: tuple[PurePosixPath, ...] = ()


def reported_file_path(
    torrent: TorrentSnapshot,
    relative_path: str | PurePosixPath,
) -> PurePosixPath | None:
    """Return the file's absolute path at qBittorrent's actual storage location.

    ``content_path`` follows the active incomplete/final storage location and
    already contains a multi-file torrent's common relative root. File entries
    retain that relative root, so it must be removed before joining. Snapshots
    without ``save_path`` are legacy/test inputs and retain the older base-path
    interpretation for compatibility.
    """

    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    content_path = PurePosixPath(torrent.content_path)
    if not content_path.is_absolute() or ".." in content_path.parts:
        return None
    if not torrent.save_path:
        return content_path.joinpath(relative)

    safe_paths = [PurePosixPath(item.path) for item in torrent.files]
    if any(path.is_absolute() or ".." in path.parts for path in safe_paths):
        return None
    if len(safe_paths) == 1 and relative == safe_paths[0]:
        return content_path

    directory_parts = [path.parts[:-1] for path in safe_paths]
    common_parts = list(directory_parts[0]) if directory_parts else []
    for parts in directory_parts[1:]:
        shared_length = 0
        for left, right in zip(common_parts, parts, strict=False):
            if left != right:
                break
            shared_length += 1
        common_parts = common_parts[:shared_length]
        if not common_parts:
            break
    common_root = PurePosixPath(*common_parts) if common_parts else None
    if common_root is not None and relative.is_relative_to(common_root):
        relative = relative.relative_to(common_root)
    return content_path.joinpath(relative)


def assess_nfo_repair(
    torrent: TorrentSnapshot,
    *,
    video_threshold: float = 0.99,
) -> NFORepairAssessment:
    """Classify an incomplete torrent and retain safe NFO repair candidates."""

    if not 0 <= video_threshold <= 1:
        raise ValueError("video_threshold must be between zero and one")
    if torrent.progress >= 1:
        return NFORepairAssessment(status="complete")

    incomplete_nfos = [
        item
        for item in torrent.files
        if PurePosixPath(item.path).suffix.lower() == ".nfo" and item.progress < 1
    ]
    if not incomplete_nfos:
        return NFORepairAssessment(status="no_incomplete_nfo")

    videos = [
        item
        for item in torrent.files
        if PurePosixPath(item.path).suffix.lower() in VIDEO_SUFFIXES
    ]
    reported_paths = tuple(
        path
        for item in incomplete_nfos
        if (path := reported_file_path(torrent, item.path)) is not None
    )
    if not videos:
        return NFORepairAssessment(
            status="no_video",
            incomplete_nfo_count=len(incomplete_nfos),
            reported_nfo_paths=reported_paths,
        )
    if any(item.progress < video_threshold for item in videos):
        return NFORepairAssessment(
            status="video_incomplete",
            incomplete_nfo_count=len(incomplete_nfos),
            reported_nfo_paths=reported_paths,
        )

    missing: list[MissingNFO] = []
    for item in incomplete_nfos:
        relative = PurePosixPath(item.path)
        reported_path = reported_file_path(torrent, relative)
        if reported_path is None:
            return NFORepairAssessment(
                status="invalid_nfo_path",
                incomplete_nfo_count=len(incomplete_nfos),
                reported_nfo_paths=reported_paths,
            )
        missing.append(
            MissingNFO(
                torrent_hash=torrent.torrent_hash,
                torrent_name=torrent.name,
                file_index=item.index,
                relative_path=relative,
                reported_path=reported_path,
            )
        )
    return NFORepairAssessment(
        status="ready",
        candidates=tuple(missing),
        incomplete_nfo_count=len(incomplete_nfos),
        reported_nfo_paths=reported_paths,
    )


def find_stuck_nfos(
    torrent: TorrentSnapshot,
    *,
    video_threshold: float = 0.99,
) -> list[MissingNFO]:
    """Return NFO candidates only when the torrent is ready for NFO-only repair."""

    return list(assess_nfo_repair(torrent, video_threshold=video_threshold).candidates)


class QBitConnector:
    """Minimal async qBittorrent WebUI v2 client."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        path_mapper: PathMapper | None = None,
        timeout: float = 20.0,
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("qBittorrent base_url must be an absolute HTTP(S) URL")
        if url.path.rstrip("/"):
            raise ValueError("qBittorrent base_url must not contain a path")
        self._base_url = str(url.copy_with(path="/")).rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None
        self._path_mapper = path_mapper
        self._authenticated = username is None and password is None
        self._auth_lock = asyncio.Lock()

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _ensure_authenticated(self) -> None:
        if self._authenticated:
            return
        async with self._auth_lock:
            if self._authenticated:
                return
            if self._username is None or self._password is None:
                raise ValueError(
                    "qBittorrent username and password must be configured together"
                )
            response = await self._http.post(
                self._url("/api/v2/auth/login"),
                data={"username": self._username, "password": self._password},
            )
            response.raise_for_status()
            if response.text.strip().lower() != "ok.":
                raise httpx.HTTPStatusError(
                    "qBittorrent rejected WebUI credentials",
                    request=response.request,
                    response=response,
                )
            self._authenticated = True

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        await self._ensure_authenticated()
        response = await self._http.request(
            method,
            self._url(path),
            **kwargs,
        )
        response.raise_for_status()
        return response

    async def list_torrents(
        self, *, category: str | None = None
    ) -> list[TorrentSnapshot]:
        params = {"category": category} if category else None
        response = await self._request("GET", "/api/v2/torrents/info", params=params)
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("qBittorrent torrent list response must be an array")
        snapshots: list[TorrentSnapshot] = []
        for raw in data:
            content_path = str(raw.get("content_path", raw.get("save_path", "")))
            save_path = str(raw.get("save_path", ""))
            local_path = (
                self._path_mapper.map_path(content_path)
                if self._path_mapper is not None and content_path
                else None
            )
            snapshots.append(
                TorrentSnapshot(
                    torrent_hash=str(raw["hash"]),
                    name=str(raw["name"]),
                    category=str(raw.get("category", "")),
                    content_path=content_path,
                    save_path=save_path,
                    progress=float(raw.get("progress", 0.0)),
                    state=str(raw.get("state", "unknown")),
                    local_content_path=local_path,
                )
            )
        return snapshots

    async def list_files(self, torrent_hash: str) -> list[TorrentFile]:
        response = await self._request(
            "GET",
            "/api/v2/torrents/files",
            params={"hash": torrent_hash},
        )
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("qBittorrent file list response must be an array")
        return [
            TorrentFile(
                index=int(raw["index"]),
                path=str(raw.get("name", raw.get("path", ""))),
                size=int(raw.get("size", 0)),
                progress=float(raw.get("progress", 0.0)),
                priority=int(raw.get("priority", 0)),
            )
            for raw in data
        ]

    async def get_torrent(self, torrent_hash: str) -> TorrentSnapshot:
        response = await self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": torrent_hash},
        )
        data = response.json()
        if not isinstance(data, list) or not data:
            raise LookupError(f"qBittorrent torrent was not found: {torrent_hash}")
        raw = data[0]
        content_path = str(raw.get("content_path", raw.get("save_path", "")))
        save_path = str(raw.get("save_path", ""))
        files = await self.list_files(torrent_hash)
        local_path = (
            self._path_mapper.map_path(content_path)
            if self._path_mapper is not None and content_path
            else None
        )
        return TorrentSnapshot(
            torrent_hash=str(raw["hash"]),
            name=str(raw["name"]),
            category=str(raw.get("category", "")),
            content_path=content_path,
            save_path=save_path,
            progress=float(raw.get("progress", 0.0)),
            state=str(raw.get("state", "unknown")),
            files=files,
            local_content_path=local_path,
        )

    async def set_file_priority(
        self,
        torrent_hash: str,
        file_ids: list[int],
        priority: int,
    ) -> None:
        if not file_ids:
            raise ValueError("at least one qBittorrent file id is required")
        await self._request(
            "POST",
            "/api/v2/torrents/filePrio",
            data={
                "hash": torrent_hash,
                "id": "|".join(str(file_id) for file_id in file_ids),
                "priority": str(priority),
            },
        )

    async def force_recheck(self, torrent_hash: str) -> None:
        await self._request(
            "POST",
            "/api/v2/torrents/recheck",
            data={"hashes": torrent_hash},
        )

    async def resume(self, torrent_hash: str) -> None:
        data = {"hashes": torrent_hash}
        try:
            await self._request(
                "POST",
                "/api/v2/torrents/start",
                data=data,
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {404, 405}:
                raise
            await self._request(
                "POST",
                "/api/v2/torrents/resume",
                data=data,
            )

    async def healthcheck(self) -> ConnectorHealth:
        from backend.connectors.health import ConnectorHealth

        try:
            response = await self._request("GET", "/api/v2/app/version")
        except (httpx.HTTPError, OSError):
            return ConnectorHealth(healthy=False, detail="connection failed")
        return ConnectorHealth(healthy=True, version=response.text.strip())

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
