"""Streaming SHA-256 with path/size/mtime cache semantics."""

from __future__ import annotations

import hashlib
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


@dataclass(frozen=True, slots=True)
class HashResult:
    digest: str | None
    bytes_hashed: int
    cache_hit: bool
    skipped_reason: str | None = None


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
