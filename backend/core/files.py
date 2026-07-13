"""Safe path mapping and byte-exact atomic NFO file operations."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    """Raised when a connector path escapes configured media roots."""


class WriteDisposition(StrEnum):
    WRITTEN = "written"
    EXISTS = "exists"


class MismatchCleanupPolicy(StrEnum):
    KEEP = "keep"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class PathMapping:
    remote_root: str
    local_root: Path

    def __post_init__(self) -> None:
        remote = PurePosixPath(self.remote_root)
        if not remote.is_absolute() or ".." in remote.parts:
            raise UnsafePathError("mapping remote_root must be an absolute safe path")
        object.__setattr__(self, "remote_root", str(remote))
        object.__setattr__(self, "local_root", Path(self.local_root))


@dataclass(frozen=True, slots=True)
class WriteResult:
    path: Path
    disposition: WriteDisposition


def _absolute_lexical(path: Path) -> Path:
    """Return an absolute path without resolving any symlink component."""

    return Path(os.path.abspath(os.fspath(path)))


def _root_pairs(allowed_roots: Iterable[Path]) -> tuple[tuple[Path, Path], ...]:
    roots = tuple(_absolute_lexical(Path(root)) for root in allowed_roots)
    if not roots:
        raise UnsafePathError("at least one allowed root is required")
    pairs: list[tuple[Path, Path]] = []
    for root in roots:
        if root.is_symlink():
            raise UnsafePathError(f"allowed root must not be a symlink: {root}")
        pairs.append((root, root.resolve(strict=False)))
    return tuple(pairs)


def _resolved_roots(allowed_roots: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(resolved for _, resolved in _root_pairs(allowed_roots))


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _ensure_allowed(
    path: Path,
    allowed_roots: Iterable[Path],
    *,
    require_nfo: bool = False,
) -> Path:
    lexical = _absolute_lexical(path)
    if require_nfo and lexical.suffix.lower() != ".nfo":
        raise UnsafePathError("file operation is restricted to lexical .nfo targets")

    candidates: list[tuple[Path, Path]] = []
    for raw_root, resolved_root in _root_pairs(allowed_roots):
        if _is_within(lexical, raw_root):
            relative = lexical.relative_to(raw_root)
            candidates.append((resolved_root.joinpath(*relative.parts), resolved_root))
        elif _is_within(lexical, resolved_root):
            candidates.append((lexical, resolved_root))
    if not candidates:
        raise UnsafePathError(f"path is outside allowed roots: {path}")

    target, root = max(candidates, key=lambda item: len(item[1].parts))
    resolved_target = target.resolve(strict=False)
    if not _is_within(resolved_target, root):
        raise UnsafePathError(f"path follows a symlink outside allowed roots: {path}")

    current = root
    for component in target.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise UnsafePathError(f"path contains a symlink component: {path}")
    return target


def _open_parent_directory(parent: Path, allowed_roots: Iterable[Path]) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    roots = [
        root for root in _resolved_roots(allowed_roots) if _is_within(parent, root)
    ]
    if not roots:
        raise UnsafePathError(f"target parent is outside allowed roots: {parent}")
    root = max(roots, key=lambda candidate: len(candidate.parts))
    descriptor: int | None = None
    try:
        descriptor = os.open(root, flags)
        for component in parent.relative_to(root).parts:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise UnsafePathError(
            f"target parent is not a safe directory: {parent}"
        ) from exc


def _entry_stat(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _validate_nfo_entry(entry: os.stat_result | None, target: Path) -> None:
    if entry is None:
        return
    if stat.S_ISLNK(entry.st_mode):
        raise UnsafePathError(f"NFO target must not be a symlink: {target}")
    if not stat.S_ISREG(entry.st_mode):
        raise UnsafePathError(f"NFO target must be a regular file: {target}")


class PathMapper:
    """Map connector-reported POSIX paths into locally mounted media roots."""

    def __init__(
        self,
        *,
        mappings: Iterable[PathMapping],
        allowed_roots: Iterable[Path],
    ) -> None:
        self._allowed_roots = _resolved_roots(allowed_roots)
        self._mappings = tuple(
            sorted(
                mappings,
                key=lambda mapping: len(PurePosixPath(mapping.remote_root).parts),
                reverse=True,
            )
        )
        if not self._mappings:
            raise UnsafePathError("at least one path mapping is required")

    def map_path(self, reported_path: str | PurePosixPath) -> Path:
        remote_path = PurePosixPath(reported_path)
        if not remote_path.is_absolute():
            raise UnsafePathError("connector path must be absolute and mapped")
        if ".." in remote_path.parts:
            raise UnsafePathError("path traversal outside allowed roots is forbidden")

        for mapping in self._mappings:
            remote_root = PurePosixPath(mapping.remote_root)
            if not remote_path.is_relative_to(remote_root):
                continue
            relative = remote_path.relative_to(remote_root)
            mapping_root = _ensure_allowed(
                Path(mapping.local_root),
                self._allowed_roots,
            )
            local_path = mapping_root.joinpath(*relative.parts)
            return _ensure_allowed(local_path, self._allowed_roots)
        raise UnsafePathError(
            f"connector path has no configured mapping: {reported_path}"
        )


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    allowed_roots: Iterable[Path],
    overwrite: bool = False,
) -> WriteResult:
    """Atomically write raw bytes while protecting existing non-empty files."""

    roots = tuple(Path(root) for root in allowed_roots)
    target = _ensure_allowed(Path(path), roots, require_nfo=True)
    parent = _ensure_allowed(target.parent, roots)
    if not parent.exists() or not parent.is_dir():
        raise UnsafePathError(f"target parent directory does not exist: {parent}")
    parent_fd = _open_parent_directory(parent, roots)
    temporary_name: str | None = None
    try:
        existing = _entry_stat(parent_fd, target.name)
        _validate_nfo_entry(existing, target)
        if existing is not None and existing.st_size > 0 and not overwrite:
            return WriteResult(target, WriteDisposition.EXISTS)

        descriptor: int | None = None
        for _ in range(100):
            candidate = f".{target.name}.{secrets.token_hex(8)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if descriptor is None or temporary_name is None:
            raise FileExistsError("could not allocate a unique NFO temporary file")

        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

        current = _entry_stat(parent_fd, target.name)
        _validate_nfo_entry(current, target)
        if current is not None and current.st_size > 0 and not overwrite:
            return WriteResult(target, WriteDisposition.EXISTS)
        if overwrite or current is not None:
            os.replace(
                temporary_name,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = None
        else:
            try:
                os.link(
                    temporary_name,
                    target.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                return WriteResult(target, WriteDisposition.EXISTS)
        os.fsync(parent_fd)
        return WriteResult(target, WriteDisposition.WRITTEN)
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)


def cleanup_nfo(
    path: Path,
    *,
    policy: MismatchCleanupPolicy,
    allowed_roots: Iterable[Path],
) -> bool:
    """Apply mismatch cleanup without ever permitting a media-file deletion."""

    roots = tuple(Path(root) for root in allowed_roots)
    target = _ensure_allowed(Path(path), roots, require_nfo=True)
    if policy is MismatchCleanupPolicy.KEEP:
        return False
    parent = _ensure_allowed(target.parent, roots)
    parent_fd = _open_parent_directory(parent, roots)
    try:
        existing = _entry_stat(parent_fd, target.name)
        _validate_nfo_entry(existing, target)
        if existing is None:
            return False
        os.unlink(target.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)
