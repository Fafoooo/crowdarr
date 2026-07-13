"""Independent NFO, MediaInfo, and file-list contribution orchestration."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from backend.connectors.health import sanitized_error
from backend.core.library import sanitize_release_name

LOGGER = logging.getLogger(__name__)

ComponentStatus = Literal["success", "failed", "skipped"]
ContributionStatus = Literal["success", "partial", "failed", "skipped"]

_CATEGORY_NAMES = {
    category.casefold(): category
    for category in (
        "Movies",
        "TV",
        "Games",
        "Software",
        "Music",
        "Books",
        "Audiobooks",
        "Other",
        "Unknown",
    )
}
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class CrowdNFOUploader(Protocol):
    async def upload_nfo(self, **kwargs: Any) -> object: ...

    async def upload_mediainfo(self, **kwargs: Any) -> object: ...

    async def upload_filelist(self, **kwargs: Any) -> object: ...


class MediaInfoInspector(Protocol):
    async def inspect(self, media_path: Path) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ContributionItem:
    release_name: str
    media_path: Path
    nfo_path: Path | None = None
    source_category: str | None = None
    media_sha256: str | None = None
    filelist: Sequence[Mapping[str, object]] = ()

    def __post_init__(self) -> None:
        release_name = sanitize_release_name(self.release_name)
        if release_name is None:
            raise ValueError("release_name is unsafe")
        media_sha256 = self.media_sha256
        if media_sha256 is not None and not _SHA256.fullmatch(media_sha256):
            raise ValueError("media_sha256 must be a complete SHA-256 digest")
        object.__setattr__(self, "release_name", release_name)
        object.__setattr__(self, "media_path", Path(self.media_path))
        if self.nfo_path is not None:
            object.__setattr__(self, "nfo_path", Path(self.nfo_path))
        if media_sha256 is not None:
            object.__setattr__(self, "media_sha256", media_sha256.lower())


@dataclass(frozen=True, slots=True)
class ContributionComponentResult:
    status: ComponentStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ContributionResult:
    status: ContributionStatus
    components: Mapping[str, ContributionComponentResult] = field(default_factory=dict)


def _normalize_filelist(
    entries: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for entry in entries:
        raw_path = entry.get("file_path", entry.get("filePath"))
        raw_size = entry.get("file_size_bytes", entry.get("fileSizeBytes"))
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("file-list path is required")
        path = PurePosixPath(raw_path.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or str(path) == ".":
            raise ValueError("file-list path must be relative and safe")
        if any(not character.isprintable() for character in str(path)):
            raise ValueError("file-list path contains control characters")
        if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
            raise ValueError("file-list size must be a non-negative integer")
        normalized.append(
            {
                "file_path": path.as_posix(),
                "file_size_bytes": raw_size,
            }
        )
    return normalized


class ContributionService:
    """Upload enabled contribution components without cross-component failure."""

    def __init__(
        self,
        *,
        crowdnfo: CrowdNFOUploader,
        mediainfo: MediaInfoInspector,
        category_mapping: Mapping[str, str] | None = None,
    ) -> None:
        self._crowdnfo = crowdnfo
        self._mediainfo = mediainfo
        self._category_mapping: dict[str, str] = {}
        for source, destination in (category_mapping or {}).items():
            source_key = source.strip().casefold()
            mapped = _CATEGORY_NAMES.get(destination.strip().casefold())
            if not source_key or mapped is None:
                raise ValueError("category mapping contains an invalid category")
            self._category_mapping[source_key] = mapped

    def _category_for(self, source: str | None) -> str:
        if not source:
            return "Unknown"
        key = source.strip().casefold()
        return self._category_mapping.get(
            key,
            _CATEGORY_NAMES.get(key, "Unknown"),
        )

    async def _run_component(
        self,
        name: str,
        operation: Callable[[], Awaitable[object]],
    ) -> ContributionComponentResult:
        try:
            await operation()
            return ContributionComponentResult("success")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            safe_error = sanitized_error(error)
            LOGGER.warning("CrowdNFO %s contribution failed (%s)", name, safe_error)
            return ContributionComponentResult("failed", safe_error)

    async def contribute(
        self,
        item: ContributionItem,
        *,
        include_nfo: bool,
        include_mediainfo: bool,
        include_filelist: bool,
    ) -> ContributionResult:
        category = self._category_for(item.source_category)
        components: dict[str, ContributionComponentResult] = {}
        nfo_path = item.nfo_path

        if include_nfo and nfo_path is not None:

            async def upload_nfo() -> object:
                resolved_media = await asyncio.to_thread(
                    item.media_path.resolve, strict=True
                )
                media_parent = resolved_media.parent
                if nfo_path.suffix.casefold() != ".nfo":
                    raise ValueError("contribution NFO must use the .nfo suffix")
                resolved_nfo = await asyncio.to_thread(nfo_path.resolve, strict=True)
                if resolved_nfo.parent != media_parent:
                    raise ValueError("contribution NFO must be beside the media file")
                content = await asyncio.to_thread(resolved_nfo.read_bytes)
                if not content:
                    raise ValueError("contribution NFO cannot be empty")
                return await self._crowdnfo.upload_nfo(
                    release_name=item.release_name,
                    filename=nfo_path.name,
                    content=content,
                    media_sha256=item.media_sha256,
                    category=category,
                )

            components["nfo"] = await self._run_component("nfo", upload_nfo)
        else:
            components["nfo"] = ContributionComponentResult("skipped")

        if include_mediainfo:

            async def upload_mediainfo() -> object:
                content = await self._mediainfo.inspect(item.media_path)
                if not content:
                    raise ValueError("MediaInfo output cannot be empty")
                return await self._crowdnfo.upload_mediainfo(
                    release_name=item.release_name,
                    filename=f"{item.release_name}.json",
                    content=content,
                    media_sha256=item.media_sha256,
                    category=category,
                )

            components["mediainfo"] = await self._run_component(
                "mediainfo", upload_mediainfo
            )
        else:
            components["mediainfo"] = ContributionComponentResult("skipped")

        if include_filelist and item.filelist:

            async def upload_filelist() -> object:
                entries = _normalize_filelist(item.filelist)
                return await self._crowdnfo.upload_filelist(
                    release_name=item.release_name,
                    entries=entries,
                    media_sha256=item.media_sha256,
                    category=category,
                )

            components["filelist"] = await self._run_component(
                "filelist", upload_filelist
            )
        else:
            components["filelist"] = ContributionComponentResult("skipped")

        statuses = [component.status for component in components.values()]
        successes = statuses.count("success")
        failures = statuses.count("failed")
        if failures and successes:
            status: ContributionStatus = "partial"
        elif failures:
            status = "failed"
        elif successes:
            status = "success"
        else:
            status = "skipped"
        return ContributionResult(status=status, components=components)
