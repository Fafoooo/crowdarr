"""Byte-exact qBittorrent NFO repair and verification loop."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from backend.connectors.qbit import (
    VIDEO_SUFFIXES,
    MissingNFO,
    TorrentFile,
    TorrentSnapshot,
    reported_file_path,
)
from backend.core.files import (
    MismatchCleanupPolicy,
    UnsafePathError,
    atomic_write_bytes,
    cleanup_nfo,
)
from backend.core.hashing import HashResult


class RepairStatus(StrEnum):
    SUCCESS = "success"
    MISMATCH = "mismatch"
    TIMEOUT = "timeout"
    DRY_RUN = "dry_run"
    PLACED_UNVERIFIED = "placed_unverified"
    VERIFIED_INCOMPLETE = "verified_incomplete"


@dataclass(frozen=True, slots=True)
class RepairResult:
    status: RepairStatus
    target_path: Path
    verified: bool = False
    seeding: bool = False
    retryable: bool = False
    message: str = ""


class CrowdNFOClientProtocol(Protocol):
    async def download_nfo(
        self,
        *,
        release_name: str,
        media_sha256: str | None = None,
    ) -> bytes: ...


class QBitRepairProtocol(Protocol):
    async def set_file_priority(
        self, torrent_hash: str, file_ids: list[int], priority: int
    ) -> None: ...

    async def force_recheck(self, torrent_hash: str) -> None: ...

    async def get_torrent(self, torrent_hash: str) -> TorrentSnapshot: ...

    async def resume(self, torrent_hash: str) -> TorrentSnapshot | None: ...


class PathMapperProtocol(Protocol):
    def map_path(self, reported_path: str | Any) -> Path: ...


class HashServiceProtocol(Protocol):
    async def hash_file(self, path: Path) -> HashResult: ...


AtomicWriter = Callable[..., object]


class TorrentRepairService:
    """Place one NFO, recheck its torrent, and verify byte-level acceptance."""

    _SEEDING_STATES = frozenset({"uploading", "stalledup", "forcedup"})

    def __init__(
        self,
        *,
        crowdnfo: CrowdNFOClientProtocol,
        qbit: QBitRepairProtocol,
        path_mapper: PathMapperProtocol,
        atomic_writer: AtomicWriter | None = None,
        allowed_roots: Iterable[Path] | None = None,
        poll_interval: float = 2.0,
        recheck_timeout: float = 300.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        dry_run: bool = False,
        keep_mismatch: bool = True,
        auto_recheck: bool = True,
        hash_service: HashServiceProtocol | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if recheck_timeout <= 0:
            raise ValueError("recheck_timeout must be positive")
        self._crowdnfo = crowdnfo
        self._qbit = qbit
        self._path_mapper = path_mapper
        self._writer = atomic_writer
        self._allowed_roots = tuple(Path(root) for root in (allowed_roots or ()))
        self._poll_interval = poll_interval
        self._recheck_timeout = recheck_timeout
        self._sleep = sleep
        self._monotonic = monotonic
        self._dry_run = dry_run
        self._keep_mismatch = keep_mismatch
        self._auto_recheck = auto_recheck
        self._hash_service = hash_service

    def _write(self, target: Path, payload: bytes) -> None:
        if self._writer is not None:
            self._writer(target, payload, overwrite=True)
            return
        if not self._allowed_roots:
            raise ValueError("allowed_roots are required for the default atomic writer")
        atomic_write_bytes(
            target,
            payload,
            allowed_roots=self._allowed_roots,
            overwrite=True,
        )

    def _cleanup_mismatch(self, target: Path, payload: bytes) -> None:
        if self._keep_mismatch:
            return
        if target.suffix.lower() != ".nfo":
            raise ValueError("repair cleanup is restricted to NFO files")
        if self._allowed_roots:
            cleanup_nfo(
                target,
                policy=MismatchCleanupPolicy.REMOVE,
                allowed_roots=self._allowed_roots,
                expected_sha256=hashlib.sha256(payload).digest(),
            )

    @staticmethod
    def _is_checking(snapshot: TorrentSnapshot) -> bool:
        return snapshot.state.lower().startswith("checking")

    @staticmethod
    def _file_verified(snapshot: TorrentSnapshot, candidate: MissingNFO) -> bool:
        return any(
            item.index == candidate.file_index and item.progress >= 1
            for item in snapshot.files
        )

    @classmethod
    def _files_verified(
        cls,
        snapshot: TorrentSnapshot,
        candidates: Sequence[MissingNFO],
    ) -> bool:
        return all(cls._file_verified(snapshot, candidate) for candidate in candidates)

    @classmethod
    def _is_seeding(cls, snapshot: TorrentSnapshot | None) -> bool:
        return snapshot is not None and snapshot.state.lower() in cls._SEEDING_STATES

    @staticmethod
    def _primary_video(files: list[TorrentFile]) -> TorrentFile | None:
        videos = [
            item
            for item in files
            if PurePosixPath(item.path).suffix.lower() in VIDEO_SUFFIXES
            and item.progress >= 1
        ]
        return max(videos, key=lambda item: item.size, default=None)

    async def _media_sha256(self, candidate: MissingNFO) -> str | None:
        if self._hash_service is None:
            return None
        snapshot = await self._qbit.get_torrent(candidate.torrent_hash)
        video = self._primary_video(snapshot.files)
        if video is None:
            return None
        reported_path = reported_file_path(snapshot, video.path)
        if reported_path is None:
            return None
        local_path = self._path_mapper.map_path(reported_path)
        result = await self._hash_service.hash_file(local_path)
        return result.digest

    async def _wait_for_recheck(
        self,
        candidates: Sequence[MissingNFO],
        *,
        deadline: float,
    ) -> TorrentSnapshot | None:
        candidate = candidates[0]
        checking_started = False
        while True:
            snapshot = await self._qbit.get_torrent(candidate.torrent_hash)
            if self._is_checking(snapshot):
                checking_started = True
            elif checking_started or self._files_verified(snapshot, candidates):
                return snapshot

            if self._monotonic() >= deadline:
                return None
            await self._sleep(self._poll_interval)

    async def _wait_for_seeding(
        self,
        candidate: MissingNFO,
        *,
        initial: TorrentSnapshot | None,
        deadline: float,
    ) -> TorrentSnapshot | None:
        snapshot = initial
        while True:
            if snapshot is None:
                snapshot = await self._qbit.get_torrent(candidate.torrent_hash)
            if self._is_seeding(snapshot):
                return snapshot
            if self._monotonic() >= deadline:
                return None
            await self._sleep(self._poll_interval)
            snapshot = await self._qbit.get_torrent(candidate.torrent_hash)

    @staticmethod
    def _validate_batch(candidates: Sequence[MissingNFO]) -> tuple[MissingNFO, ...]:
        batch = tuple(candidates)
        if not batch:
            raise ValueError("at least one missing NFO is required")
        torrent_hashes = {candidate.torrent_hash for candidate in batch}
        torrent_names = {candidate.torrent_name for candidate in batch}
        file_indexes = {candidate.file_index for candidate in batch}
        if len(torrent_hashes) != 1 or len(torrent_names) != 1:
            raise ValueError("all repair candidates must belong to one torrent")
        if len(file_indexes) != len(batch):
            raise ValueError("repair candidates must have unique file indexes")
        return batch

    def _target_for(self, candidate: MissingNFO) -> Path:
        target = self._path_mapper.map_path(candidate.reported_path)
        if target.suffix.lower() != ".nfo":
            raise UnsafePathError(
                "torrent repair target must remain a lexical .nfo path"
            )
        return target

    async def repair_many(
        self,
        candidates: Sequence[MissingNFO],
    ) -> list[RepairResult]:
        """Place every missing NFO for one torrent before issuing one recheck."""

        batch = self._validate_batch(candidates)
        targets = [self._target_for(candidate) for candidate in batch]
        if self._dry_run:
            return [
                RepairResult(
                    status=RepairStatus.DRY_RUN,
                    target_path=target,
                    message="dry-run: no file or torrent state was changed",
                )
                for target in targets
            ]

        media_sha256 = await self._media_sha256(batch[0])
        payloads = [
            await self._crowdnfo.download_nfo(
                release_name=candidate.torrent_name,
                media_sha256=media_sha256,
            )
            for candidate in batch
        ]
        for target, payload in zip(targets, payloads, strict=True):
            self._write(target, payload)
        await self._qbit.set_file_priority(
            batch[0].torrent_hash,
            [candidate.file_index for candidate in batch],
            priority=1,
        )
        if not self._auto_recheck:
            return [
                RepairResult(
                    status=RepairStatus.PLACED_UNVERIFIED,
                    target_path=target,
                    message=(
                        "NFO placed but not verified because auto-recheck is disabled"
                    ),
                )
                for target in targets
            ]

        await self._qbit.force_recheck(batch[0].torrent_hash)

        deadline = self._monotonic() + self._recheck_timeout
        snapshot = await self._wait_for_recheck(batch, deadline=deadline)
        if snapshot is None:
            return [
                RepairResult(
                    status=RepairStatus.TIMEOUT,
                    target_path=target,
                    retryable=True,
                    message="torrent recheck did not start and finish before timeout",
                )
                for target in targets
            ]

        verification = [self._file_verified(snapshot, candidate) for candidate in batch]
        torrent_complete = snapshot.progress >= 1 and all(verification)
        if not torrent_complete:
            results: list[RepairResult] = []
            for target, payload, verified in zip(
                targets, payloads, verification, strict=True
            ):
                if verified:
                    results.append(
                        RepairResult(
                            status=RepairStatus.VERIFIED_INCOMPLETE,
                            target_path=target,
                            verified=True,
                            retryable=True,
                            message=(
                                "NFO verified, but other torrent data remains "
                                "incomplete"
                            ),
                        )
                    )
                    continue
                self._cleanup_mismatch(target, payload)
                results.append(
                    RepairResult(
                        status=RepairStatus.MISMATCH,
                        target_path=target,
                        retryable=True,
                        message="NFO mismatch: file remained incomplete after recheck",
                    )
                )
            return results

        resumed = await self._qbit.resume(batch[0].torrent_hash)
        seeding = await self._wait_for_seeding(
            batch[0],
            initial=resumed,
            deadline=self._monotonic() + self._recheck_timeout,
        )
        if seeding is not None:
            return [
                RepairResult(
                    status=RepairStatus.SUCCESS,
                    target_path=target,
                    verified=True,
                    seeding=True,
                    message="NFO verified; torrent is seeding",
                )
                for target in targets
            ]
        return [
            RepairResult(
                status=RepairStatus.TIMEOUT,
                target_path=target,
                verified=True,
                retryable=True,
                message=(
                    "torrent verified but did not enter an active seeding state "
                    "before timeout"
                ),
            )
            for target in targets
        ]

    async def repair(self, candidate: MissingNFO) -> RepairResult:
        """Backward-compatible single-candidate repair entry point."""

        return (await self.repair_many((candidate,)))[0]
