"""Safe path mapping and byte-exact atomic NFO file operations."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
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


def _resolved_roots(allowed_roots: Iterable[Path]) -> tuple[Path, ...]:
    roots = tuple(Path(root).resolve(strict=False) for root in allowed_roots)
    if not roots:
        raise UnsafePathError("at least one allowed root is required")
    return roots


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _ensure_allowed(path: Path, allowed_roots: Iterable[Path]) -> Path:
    resolved = path.resolve(strict=False)
    roots = _resolved_roots(allowed_roots)
    if not any(_is_within(resolved, root) for root in roots):
        raise UnsafePathError(f"path is outside allowed roots: {path}")
    return resolved


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
            local_path = Path(mapping.local_root).joinpath(*relative.parts)
            resolved = local_path.resolve(strict=False)
            if not any(
                _is_within(resolved, allowed_root)
                for allowed_root in self._allowed_roots
            ):
                raise UnsafePathError(
                    "mapped path follows a symlink or points outside allowed roots"
                )
            return resolved
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

    target = _ensure_allowed(Path(path), allowed_roots)
    parent = _ensure_allowed(target.parent, allowed_roots)
    if not parent.exists() or not parent.is_dir():
        raise UnsafePathError(f"target parent directory does not exist: {parent}")
    if target.exists() and target.stat().st_size > 0 and not overwrite:
        return WriteResult(target, WriteDisposition.EXISTS)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

        if overwrite or (target.exists() and target.stat().st_size == 0):
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError:
                return WriteResult(target, WriteDisposition.EXISTS)
            finally:
                temporary.unlink(missing_ok=True)
        return WriteResult(target, WriteDisposition.WRITTEN)
    finally:
        temporary.unlink(missing_ok=True)


def cleanup_nfo(
    path: Path,
    *,
    policy: MismatchCleanupPolicy,
    allowed_roots: Iterable[Path],
) -> bool:
    """Apply mismatch cleanup without ever permitting a media-file deletion."""

    target = _ensure_allowed(Path(path), allowed_roots)
    if target.suffix.lower() != ".nfo":
        raise UnsafePathError("cleanup is restricted to .nfo files")
    if policy is MismatchCleanupPolicy.KEEP:
        return False
    existed = target.exists()
    target.unlink(missing_ok=True)
    return existed
