from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from backend.core.files import (
    MismatchCleanupPolicy,
    PathMapper,
    PathMapping,
    UnsafePathError,
    WriteDisposition,
    atomic_write_bytes,
    cleanup_nfo,
)
from backend.core.hashing import HashCacheKey, stream_sha256


def test_path_mapper_uses_longest_component_prefix(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    cross_seed_root = tmp_path / "cross-seeds"
    data_root.mkdir()
    cross_seed_root.mkdir()
    mapper = PathMapper(
        mappings=[
            PathMapping(remote_root="/data", local_root=data_root),
            PathMapping(
                remote_root="/data/cross-seeds",
                local_root=cross_seed_root,
            ),
        ],
        allowed_roots=[data_root, cross_seed_root],
    )

    assert mapper.map_path("/data/movies/Film/Film.nfo") == (
        data_root / "movies/Film/Film.nfo"
    )
    assert mapper.map_path("/data/cross-seeds/Release/Release.nfo") == (
        cross_seed_root / "Release/Release.nfo"
    )


def test_path_mapper_rejects_unmapped_prefix_traversal_and_disallowed_root(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    safe_mapper = PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=allowed)],
        allowed_roots=[allowed],
    )
    unsafe_mapper = PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=outside)],
        allowed_roots=[allowed],
    )

    with pytest.raises(UnsafePathError, match="mapped|mapping"):
        safe_mapper.map_path("/database/not-a-data-child/file.nfo")
    with pytest.raises(UnsafePathError, match="traversal|outside|allowed"):
        safe_mapper.map_path("/data/../../etc/passwd")
    with pytest.raises(UnsafePathError, match="outside|allowed"):
        unsafe_mapper.map_path("/data/Release/Release.nfo")


def test_path_mapper_rejects_symlink_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    mapper = PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=allowed)],
        allowed_roots=[allowed],
    )

    with pytest.raises(UnsafePathError, match="symlink|outside|allowed"):
        mapper.map_path("/data/escape/release.nfo")


def test_atomic_write_preserves_raw_bytes_and_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "release" / "release.nfo"
    target.parent.mkdir()
    payload = b"\x00\xff\xfeCP437:\x80\r\nExact bytes\r\n"

    result = atomic_write_bytes(
        target,
        payload,
        allowed_roots=[tmp_path],
        overwrite=False,
    )

    assert result.disposition is WriteDisposition.WRITTEN
    assert result.path == target
    assert target.read_bytes() == payload
    assert list(target.parent.iterdir()) == [target]


def test_atomic_write_does_not_overwrite_existing_non_empty_nfo(
    tmp_path: Path,
) -> None:
    target = tmp_path / "release.nfo"
    original = b"already complete\r\n"
    target.write_bytes(original)

    result = atomic_write_bytes(
        target,
        b"replacement must not win",
        allowed_roots=[tmp_path],
        overwrite=False,
    )

    assert result.disposition is WriteDisposition.EXISTS
    assert target.read_bytes() == original


def test_mismatch_cleanup_policy_can_keep_or_remove_only_nfo(tmp_path: Path) -> None:
    nfo = tmp_path / "release.nfo"
    media = tmp_path / "release.mkv"
    nfo.write_bytes(b"candidate")
    media.write_bytes(b"never delete media")

    kept = cleanup_nfo(
        nfo,
        policy=MismatchCleanupPolicy.KEEP,
        allowed_roots=[tmp_path],
    )
    assert kept is False
    assert nfo.exists()

    removed = cleanup_nfo(
        nfo,
        policy=MismatchCleanupPolicy.REMOVE,
        allowed_roots=[tmp_path],
    )
    assert removed is True
    assert not nfo.exists()
    assert media.read_bytes() == b"never delete media"

    with pytest.raises(UnsafePathError, match="nfo"):
        cleanup_nfo(
            media,
            policy=MismatchCleanupPolicy.REMOVE,
            allowed_roots=[tmp_path],
        )


class RecordingHashCache:
    def __init__(self) -> None:
        self.values: dict[HashCacheKey, str] = {}
        self.gets: list[HashCacheKey] = []
        self.puts: list[tuple[HashCacheKey, str]] = []

    def get(self, key: HashCacheKey) -> str | None:
        self.gets.append(key)
        return self.values.get(key)

    def put(self, key: HashCacheKey, digest: str) -> None:
        self.puts.append((key, digest))
        self.values[key] = digest


def test_stream_hashes_full_content_and_reuses_path_size_mtime_cache(
    tmp_path: Path,
) -> None:
    media = tmp_path / "release.mkv"
    payload = (b"0123456789abcdef" * 128) + b"tail"
    media.write_bytes(payload)
    cache = RecordingHashCache()

    first = stream_sha256(
        media,
        max_size=len(payload),
        cache=cache,
        chunk_size=17,
    )
    second = stream_sha256(
        media,
        max_size=len(payload),
        cache=cache,
        chunk_size=17,
    )

    expected = hashlib.sha256(payload).hexdigest()
    assert first.digest == expected
    assert first.bytes_hashed == len(payload)
    assert first.cache_hit is False
    assert second.digest == expected
    assert second.bytes_hashed == 0
    assert second.cache_hit is True
    assert len(cache.puts) == 1
    key = cache.puts[0][0]
    assert Path(key.path) == media.resolve()
    assert key.size == len(payload)
    assert key.mtime_ns == media.stat().st_mtime_ns


def test_hash_cache_invalidates_when_same_size_file_mtime_changes(
    tmp_path: Path,
) -> None:
    media = tmp_path / "release.mkv"
    media.write_bytes(b"AAAA")
    cache = RecordingHashCache()
    first = stream_sha256(media, max_size=4, cache=cache, chunk_size=2)
    original_mtime = media.stat().st_mtime_ns

    media.write_bytes(b"BBBB")
    changed_mtime = original_mtime + 1_000_000_000
    os.utime(media, ns=(changed_mtime, changed_mtime))
    second = stream_sha256(media, max_size=4, cache=cache, chunk_size=2)

    assert first.digest == hashlib.sha256(b"AAAA").hexdigest()
    assert second.digest == hashlib.sha256(b"BBBB").hexdigest()
    assert second.cache_hit is False
    assert len(cache.puts) == 2
    assert cache.puts[0][0].size == cache.puts[1][0].size
    assert cache.puts[0][0].mtime_ns != cache.puts[1][0].mtime_ns


def test_oversized_file_is_a_capability_miss_and_is_never_partially_hashed(
    tmp_path: Path,
) -> None:
    media = tmp_path / "large.mkv"
    media.write_bytes(b"0123456789")
    cache = RecordingHashCache()

    result = stream_sha256(media, max_size=9, cache=cache, chunk_size=3)

    assert result.digest is None
    assert result.bytes_hashed == 0
    assert result.cache_hit is False
    assert result.skipped_reason == "max_size_exceeded"
    assert cache.puts == []
