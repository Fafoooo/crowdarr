"""Streaming SHA-256 with path/size/mtime cache semantics."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class HashCacheKey:
    path: str
    size: int
    mtime_ns: int


class HashCache(Protocol):
    def get(self, key: HashCacheKey) -> str | None: ...

    def put(self, key: HashCacheKey, digest: str) -> None: ...


class AsyncHashCache(Protocol):
    async def get_file_hash(
        self,
        *,
        path: Path,
        size: int,
        mtime_ns: int,
    ) -> str | None: ...

    async def put_file_hash(
        self,
        *,
        path: Path,
        size: int,
        mtime_ns: int,
        sha256: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class HashResult:
    digest: str | None
    bytes_hashed: int
    cache_hit: bool
    skipped_reason: str | None = None


HashFunction = Callable[..., HashResult]


def stream_sha256(
    path: Path,
    *,
    max_size: int,
    cache: HashCache | None = None,
    chunk_size: int = 1024 * 1024,
) -> HashResult:
    """Hash the complete file or return an explicit size capability miss."""

    if max_size < 0:
        raise ValueError("max_size cannot be negative")
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least one byte")

    resolved = Path(path).resolve(strict=True)
    before = resolved.stat()
    if not resolved.is_file():
        raise ValueError(f"hash target is not a regular file: {resolved}")
    if before.st_size > max_size:
        return HashResult(
            digest=None,
            bytes_hashed=0,
            cache_hit=False,
            skipped_reason="max_size_exceeded",
        )

    key = HashCacheKey(
        path=str(resolved),
        size=before.st_size,
        mtime_ns=before.st_mtime_ns,
    )
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return HashResult(digest=cached, bytes_hashed=0, cache_hit=True)

    digest = hashlib.sha256()
    bytes_hashed = 0
    with resolved.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            bytes_hashed += len(chunk)

    after = resolved.stat()
    if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise RuntimeError("file changed while it was being hashed")
    value = digest.hexdigest()
    if cache is not None:
        cache.put(key, value)
    return HashResult(digest=value, bytes_hashed=bytes_hashed, cache_hit=False)


class AsyncHashService:
    """Run full-file hashing off the event loop with bounded concurrency."""

    def __init__(
        self,
        *,
        cache: AsyncHashCache | None,
        max_size_bytes: int,
        max_concurrency: int = 2,
        hash_function: HashFunction = stream_sha256,
    ) -> None:
        if max_size_bytes < 1:
            raise ValueError("max_size_bytes must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        self._cache = cache
        self._max_size_bytes = max_size_bytes
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._hash_function = hash_function

    async def _metadata(self, path: Path) -> tuple[Path, int, int, bool]:
        resolved = await asyncio.to_thread(Path(path).resolve, strict=True)
        stat = await asyncio.to_thread(resolved.stat)
        is_file = await asyncio.to_thread(resolved.is_file)
        return resolved, stat.st_size, stat.st_mtime_ns, is_file

    async def _cached(
        self,
        *,
        path: Path,
        size: int,
        mtime_ns: int,
    ) -> str | None:
        if self._cache is None:
            return None
        return await self._cache.get_file_hash(
            path=path,
            size=size,
            mtime_ns=mtime_ns,
        )

    async def hash_file(self, path: Path) -> HashResult:
        resolved, size, mtime_ns, is_file = await self._metadata(path)
        if not is_file:
            raise ValueError(f"hash target is not a regular file: {resolved}")
        if size > self._max_size_bytes:
            return HashResult(
                digest=None,
                bytes_hashed=0,
                cache_hit=False,
                skipped_reason="max_size_exceeded",
            )

        cached = await self._cached(path=resolved, size=size, mtime_ns=mtime_ns)
        if cached is not None:
            return HashResult(digest=cached, bytes_hashed=0, cache_hit=True)

        async with self._semaphore:
            resolved, size, mtime_ns, is_file = await self._metadata(resolved)
            if not is_file:
                raise ValueError(f"hash target is not a regular file: {resolved}")
            if size > self._max_size_bytes:
                return HashResult(
                    digest=None,
                    bytes_hashed=0,
                    cache_hit=False,
                    skipped_reason="max_size_exceeded",
                )
            cached = await self._cached(path=resolved, size=size, mtime_ns=mtime_ns)
            if cached is not None:
                return HashResult(digest=cached, bytes_hashed=0, cache_hit=True)
            result = await asyncio.to_thread(
                self._hash_function,
                resolved,
                max_size=self._max_size_bytes,
                cache=None,
            )
            if result.digest is not None and self._cache is not None:
                await self._cache.put_file_hash(
                    path=resolved,
                    size=size,
                    mtime_ns=mtime_ns,
                    sha256=result.digest,
                )
            return result
