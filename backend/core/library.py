"""Connector-neutral media-library records and sidecar discovery."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_FORBIDDEN_RELEASE_CHARACTERS = frozenset('<>"/\\|?*')


def sanitize_release_name(value: object) -> str | None:
    """Return a CrowdNFO-safe release name or ``None`` for unsafe input."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 500 or candidate in {".", ".."}:
        return None
    if any(
        character in _FORBIDDEN_RELEASE_CHARACTERS or not character.isprintable()
        for character in candidate
    ):
        return None
    return candidate


@dataclass(frozen=True, slots=True)
class LibraryMediaItem:
    """A media file reported by Radarr, Sonarr, or another library source."""

    release_name: str
    local_media_path: Path
    source: str = "library"
    remote_media_path: str | None = None
    item_id: int | str | None = None

    def __post_init__(self) -> None:
        release_name = sanitize_release_name(self.release_name)
        if release_name is None:
            raise ValueError("release_name is unsafe")
        object.__setattr__(self, "release_name", release_name)
        object.__setattr__(self, "local_media_path", Path(self.local_media_path))

    @property
    def sidecar_path(self) -> Path:
        return self.local_media_path.with_suffix(".nfo")


def find_missing_sidecars(
    items: Iterable[LibraryMediaItem],
) -> list[LibraryMediaItem]:
    """Return mounted media items whose expected NFO is absent or empty."""

    missing: list[LibraryMediaItem] = []
    seen: set[Path] = set()
    for item in items:
        media_path = item.local_media_path
        if not media_path.is_file():
            continue
        sidecar = item.sidecar_path
        identity = sidecar.resolve(strict=False)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            if sidecar.is_file() and sidecar.stat().st_size > 0:
                continue
        except OSError:
            continue
        missing.append(item)
    return missing
