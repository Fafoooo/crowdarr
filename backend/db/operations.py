"""Durable operational state for activity, counters, jobs, and idempotency."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

_BUSY_TIMEOUT_MS = 5_000
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_COUNTER_NAMES = ("fetched", "matches", "misses", "repaired", "uploaded")
_COUNTER_SEMANTICS_KEY = "counter_semantics"
_COUNTER_SEMANTICS_VERSION = "2"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored operation JSON must be an object")
    return decoded


def _cache_path(path: Path) -> str:
    return str(Path(path).resolve(strict=False))


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    id: int
    event_type: str
    message: str
    details: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    kind: str
    status: str
    result: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class OperationsStore:
    """Small async SQLite store with a connection-per-operation lifecycle."""

    def __init__(self, database: Path) -> None:
        self._database = Path(database)
        self._lock = asyncio.Lock()
        self._initialized = False

    async def _connect(self) -> aiosqlite.Connection:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(
            self._database,
            timeout=_BUSY_TIMEOUT_MS / 1_000,
        )
        await connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        await connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    async def _initialize_unlocked(self) -> None:
        if self._initialized:
            return
        connection = await self._connect()
        try:
            cursor = await connection.execute("PRAGMA journal_mode=WAL")
            journal_mode = await cursor.fetchone()
            await cursor.close()
            if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
                raise RuntimeError("SQLite WAL mode could not be enabled")
            await connection.executescript("""
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_activity_created_at
                    ON activity(created_at DESC);

                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL CHECK (value >= 0)
                );

                CREATE TABLE IF NOT EXISTS operation_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_jobs_updated_at
                    ON jobs(updated_at DESC);

                CREATE TABLE IF NOT EXISTS completed_operations (
                    idempotency_key TEXT PRIMARY KEY,
                    completed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS file_hash_cache (
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK (size >= 0),
                    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
                    sha256 TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(path, size, mtime_ns)
                );
                CREATE INDEX IF NOT EXISTS ix_file_hash_cache_updated_at
                    ON file_hash_cache(updated_at DESC);
                """)
            await self._migrate_counter_semantics_unlocked(connection)
            now = _utc_now().isoformat()
            await connection.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    result = ?,
                    updated_at = ?
                WHERE status = 'running'
                """,
                (
                    json.dumps(
                        {"detail": "interrupted by application restart"},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            await connection.commit()
        finally:
            await connection.close()
        self._initialized = True

    @staticmethod
    async def _migrate_counter_semantics_unlocked(
        connection: aiosqlite.Connection,
    ) -> None:
        cursor = await connection.execute(
            "SELECT value FROM operation_metadata WHERE key = ?",
            (_COUNTER_SEMANTICS_KEY,),
        )
        version = await cursor.fetchone()
        await cursor.close()
        if version is not None and str(version[0]) == _COUNTER_SEMANTICS_VERSION:
            return

        cursor = await connection.execute(
            "SELECT event_type, message, details FROM activity ORDER BY id"
        )
        activity_rows = await cursor.fetchall()
        await cursor.close()
        cursor = await connection.execute("SELECT name, value FROM counters")
        counter_rows = await cursor.fetchall()
        await cursor.close()

        if activity_rows:
            derived = {name: 0 for name in _COUNTER_NAMES}
            repair_fetch_titles = {
                "torrent repaired",
                "torrent nfo verified",
                "nfo placed; recheck disabled",
                "nfo verified; torrent incomplete",
                "nfo verified; seeding not confirmed",
            }
            verification_failures = {"nfo mismatch", "verification timed out"}
            for event_type, message, raw_details in activity_rows:
                try:
                    details = _load_json(str(raw_details))
                except (json.JSONDecodeError, ValueError, TypeError):
                    details = {}
                event = str(event_type).casefold()
                status = str(details.get("status", "")).casefold()
                title = str(details.get("title", "")).casefold()
                detail = str(message).casefold()

                if event == "miss":
                    if detail in verification_failures:
                        derived["fetched"] += 1
                        derived["matches"] += 1
                    else:
                        derived["misses"] += 1
                    continue
                if event == "repair":
                    if title in repair_fetch_titles:
                        derived["fetched"] += 1
                        derived["matches"] += 1
                    if title == "torrent repaired" and status == "success":
                        derived["repaired"] += 1
                    continue
                if event in {"library_fetch", "sab_fetch", "qbit_fetch"}:
                    if status == "success":
                        derived["fetched"] += 1
                        derived["matches"] += 1
                    continue
                if (
                    event in {"sab_contribute", "qbit_contribute"}
                    and status == "success"
                ):
                    derived["uploaded"] += 1

            existing = {str(name): int(value) for name, value in counter_rows}
            corrected = {
                name: max(existing.get(name, 0), derived[name])
                for name in _COUNTER_NAMES
            }
            corrected["misses"] = derived["misses"]
            await connection.executemany(
                """
                INSERT INTO counters(name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value
                """,
                tuple(corrected.items()),
            )

        await connection.execute(
            """
            INSERT INTO operation_metadata(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_COUNTER_SEMANTICS_KEY, _COUNTER_SEMANTICS_VERSION),
        )

    async def initialize(self) -> None:
        async with self._lock:
            await self._initialize_unlocked()

    async def record_activity(
        self,
        *,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> ActivityRecord:
        if not event_type.strip():
            raise ValueError("event_type cannot be blank")
        if not message.strip():
            raise ValueError("message cannot be blank")
        created_at = _utc_now()
        serialized = json.dumps(details or {}, separators=(",", ":"), sort_keys=True)
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                cursor = await connection.execute(
                    """
                    INSERT INTO activity(event_type, message, details, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (event_type, message, serialized, created_at.isoformat()),
                )
                await connection.commit()
                identifier = cursor.lastrowid
                await cursor.close()
            finally:
                await connection.close()
        if identifier is None:
            raise RuntimeError("SQLite did not return an activity identifier")
        return ActivityRecord(
            id=identifier,
            event_type=event_type,
            message=message,
            details=details or {},
            created_at=created_at,
        )

    async def list_activity(self, *, limit: int = 50) -> list[ActivityRecord]:
        if limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                cursor = await connection.execute(
                    """
                    SELECT id, event_type, message, details, created_at
                    FROM activity
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            finally:
                await connection.close()
        return [
            ActivityRecord(
                id=int(row[0]),
                event_type=str(row[1]),
                message=str(row[2]),
                details=_load_json(str(row[3])),
                created_at=datetime.fromisoformat(str(row[4])),
            )
            for row in rows
        ]

    async def increment_counter(self, name: str, amount: int = 1) -> int:
        if not name.strip():
            raise ValueError("counter name cannot be blank")
        if amount < 0:
            raise ValueError("counter increment cannot be negative")
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                await connection.execute(
                    """
                    INSERT INTO counters(name, value) VALUES (?, ?)
                    ON CONFLICT(name) DO UPDATE SET value = value + excluded.value
                    """,
                    (name, amount),
                )
                cursor = await connection.execute(
                    "SELECT value FROM counters WHERE name = ?", (name,)
                )
                row = await cursor.fetchone()
                await cursor.close()
                await connection.commit()
            finally:
                await connection.close()
        if row is None:
            raise RuntimeError("counter update did not persist")
        return int(row[0])

    async def get_counters(self) -> dict[str, int]:
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                cursor = await connection.execute(
                    "SELECT name, value FROM counters ORDER BY name"
                )
                rows = await cursor.fetchall()
                await cursor.close()
            finally:
                await connection.close()
        return {str(row[0]): int(row[1]) for row in rows}

    async def create_job(
        self,
        *,
        job_id: str,
        kind: str,
        status: str = "queued",
    ) -> JobRecord:
        if not job_id.strip() or not kind.strip() or not status.strip():
            raise ValueError("job_id, kind, and status cannot be blank")
        now = _utc_now()
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                await connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, kind, status, result, created_at, updated_at
                    )
                    VALUES (?, ?, ?, '{}', ?, ?)
                    """,
                    (job_id, kind, status, now.isoformat(), now.isoformat()),
                )
                await connection.commit()
            finally:
                await connection.close()
        return JobRecord(job_id, kind, status, {}, now, now)

    async def update_job(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> JobRecord:
        if not status.strip():
            raise ValueError("job status cannot be blank")
        now = _utc_now()
        serialized = json.dumps(result or {}, separators=(",", ":"), sort_keys=True)
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                cursor = await connection.execute(
                    """
                    UPDATE jobs SET status = ?, result = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (status, serialized, now.isoformat(), job_id),
                )
                if cursor.rowcount != 1:
                    await cursor.close()
                    raise KeyError(job_id)
                await cursor.close()
                lookup = await connection.execute(
                    """
                    SELECT job_id, kind, status, result, created_at, updated_at
                    FROM jobs WHERE job_id = ?
                    """,
                    (job_id,),
                )
                row = await lookup.fetchone()
                await lookup.close()
                await connection.commit()
            finally:
                await connection.close()
        if row is None:
            raise KeyError(job_id)
        return self._job_from_row(row)

    @staticmethod
    def _job_from_row(row: Sequence[Any]) -> JobRecord:
        return JobRecord(
            job_id=str(row[0]),
            kind=str(row[1]),
            status=str(row[2]),
            result=_load_json(str(row[3])),
            created_at=datetime.fromisoformat(str(row[4])),
            updated_at=datetime.fromisoformat(str(row[5])),
        )

    async def get_job(self, job_id: str) -> JobRecord:
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                cursor = await connection.execute(
                    """
                    SELECT job_id, kind, status, result, created_at, updated_at
                    FROM jobs WHERE job_id = ?
                    """,
                    (job_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
            finally:
                await connection.close()
        if row is None:
            raise KeyError(job_id)
        return self._job_from_row(row)

    async def list_jobs(self, *, limit: int = 50) -> list[JobRecord]:
        if limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                cursor = await connection.execute(
                    """
                    SELECT job_id, kind, status, result, created_at, updated_at
                    FROM jobs ORDER BY updated_at DESC LIMIT ?
                    """,
                    (limit,),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            finally:
                await connection.close()
        return [self._job_from_row(row) for row in rows]

    async def was_completed(self, key: str) -> bool:
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                cursor = await connection.execute(
                    """
                    SELECT 1 FROM completed_operations WHERE idempotency_key = ?
                    """,
                    (key,),
                )
                row = await cursor.fetchone()
                await cursor.close()
            finally:
                await connection.close()
        return row is not None

    async def mark_completed(self, key: str) -> None:
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                await connection.execute(
                    """
                    INSERT OR IGNORE INTO completed_operations(
                        idempotency_key, completed_at
                    ) VALUES (?, ?)
                    """,
                    (key, _utc_now().isoformat()),
                )
                await connection.commit()
            finally:
                await connection.close()

    async def get_file_hash(
        self,
        *,
        path: Path,
        size: int,
        mtime_ns: int,
    ) -> str | None:
        if size < 0 or mtime_ns < 0:
            raise ValueError("file hash cache metadata cannot be negative")
        cache_path = _cache_path(path)
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                cursor = await connection.execute(
                    """
                    SELECT sha256
                    FROM file_hash_cache
                    WHERE path = ? AND size = ? AND mtime_ns = ?
                    """,
                    (cache_path, size, mtime_ns),
                )
                row = await cursor.fetchone()
                await cursor.close()
            finally:
                await connection.close()
        return str(row[0]) if row is not None else None

    async def put_file_hash(
        self,
        *,
        path: Path,
        size: int,
        mtime_ns: int,
        sha256: str,
    ) -> None:
        if size < 0 or mtime_ns < 0:
            raise ValueError("file hash cache metadata cannot be negative")
        if not _SHA256.fullmatch(sha256):
            raise ValueError("sha256 must be a complete SHA-256 digest")
        cache_path = _cache_path(path)
        async with self._lock:
            await self._initialize_unlocked()
            connection = await self._connect()
            try:
                await connection.execute(
                    "DELETE FROM file_hash_cache WHERE path = ?",
                    (cache_path,),
                )
                await connection.execute(
                    """
                    INSERT INTO file_hash_cache(
                        path, size, mtime_ns, sha256, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        cache_path,
                        size,
                        mtime_ns,
                        sha256.casefold(),
                        _utc_now().isoformat(),
                    ),
                )
                await connection.commit()
            finally:
                await connection.close()

    async def close(self) -> None:
        """No-op kept for a uniform lifecycle API; connections are per-operation."""
