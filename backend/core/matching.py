"""Hash-first CrowdNFO matching with verified release-name fallback."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from backend.crowdnfo.client import UnsupportedLookupError

LOGGER = logging.getLogger(__name__)


class MatchStatus(StrEnum):
    HIT = "hit"
    MISS = "miss"


class MatchStrategy(StrEnum):
    HASH = "hash"
    RELEASE_NAME = "release_name"


class MatchingMode(StrEnum):
    HASH_THEN_RELEASE_NAME = "hash_then_release_name"
    HASH_ONLY = "hash_only"
    RELEASE_NAME_ONLY = "release_name_only"


class MatchProvider(Protocol):
    async def lookup(
        self,
        *,
        media_sha256: str | None = None,
        release_name: str | None = None,
    ) -> Any | None: ...


@dataclass(frozen=True, slots=True)
class MatchResult:
    status: MatchStatus
    strategy: MatchStrategy | None = None
    release: Any | None = None
    hash_verified: bool = False
    retryable: bool = True
    reason: str | None = None


def _field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _release_hashes(release: object) -> set[str]:
    hashes: set[str] = set()
    for name in ("canonical_file_hash", "file_hash", "media_sha256", "sha256"):
        value = _field(release, name)
        if isinstance(value, str) and value:
            hashes.add(value.casefold())

    variants = _field(release, "variants")
    if isinstance(variants, Sequence) and not isinstance(variants, (str, bytes)):
        for variant in variants:
            for name in ("file_hash", "media_sha256", "sha256"):
                value = _field(variant, name)
                if isinstance(value, str) and value:
                    hashes.add(value.casefold())
    return hashes


class Matcher:
    """Resolve a release without trusting an unverified name-only candidate."""

    def __init__(
        self,
        *,
        provider: MatchProvider,
        mode: MatchingMode | str = MatchingMode.HASH_THEN_RELEASE_NAME,
    ) -> None:
        self._provider = provider
        self._mode = MatchingMode(mode)

    @staticmethod
    def _miss(reason: str, *, retryable: bool = True) -> MatchResult:
        LOGGER.info("retryable match miss: %s", reason)
        return MatchResult(
            status=MatchStatus.MISS,
            retryable=retryable,
            reason=reason,
        )

    async def match(
        self,
        *,
        media_sha256: str | None,
        release_name: str | None,
    ) -> MatchResult:
        hash_available = bool(media_sha256)
        name_available = bool(release_name and release_name.strip())

        if self._mode is not MatchingMode.RELEASE_NAME_ONLY and hash_available:
            try:
                release = await self._provider.lookup(media_sha256=media_sha256)
            except UnsupportedLookupError:
                LOGGER.info(
                    "hash-only lookup unsupported; trying release_name fallback"
                )
            else:
                if release is not None:
                    LOGGER.info("hash match hit")
                    return MatchResult(
                        status=MatchStatus.HIT,
                        strategy=MatchStrategy.HASH,
                        release=release,
                        hash_verified=True,
                        retryable=False,
                    )
                LOGGER.info("hash match miss; trying release_name fallback")

            if self._mode is MatchingMode.HASH_ONLY:
                return self._miss("hash_lookup_miss")

        if self._mode is MatchingMode.HASH_ONLY:
            return self._miss("media_hash_unavailable")
        if not name_available:
            return self._miss("release_name_unavailable")

        release = await self._provider.lookup(release_name=release_name)
        if release is None:
            return self._miss("release_not_found")

        hash_verified = False
        if media_sha256:
            hash_verified = media_sha256.casefold() in _release_hashes(release)
            if not hash_verified:
                return self._miss("release_hash_mismatch")

        LOGGER.info("release_name match hit (hash_verified=%s)", hash_verified)
        return MatchResult(
            status=MatchStatus.HIT,
            strategy=MatchStrategy.RELEASE_NAME,
            release=release,
            hash_verified=hash_verified,
            retryable=False,
        )
