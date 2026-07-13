"""Bounded and idempotent scan coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from backend.core.settings import DownloadMode


class ScanTrigger(StrEnum):
    NEW_DOWNLOAD = "new_download"
    BACKFILL = "backfill"


def mode_allows_trigger(mode: DownloadMode, trigger: ScanTrigger) -> bool:
    if mode is DownloadMode.OFF:
        return False
    if trigger is ScanTrigger.NEW_DOWNLOAD:
        return True
    return mode is DownloadMode.NEW_AND_BACKFILL


class IdempotencyStore(Protocol):
    async def was_completed(self, key: str) -> bool: ...

    async def mark_completed(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ScanResult:
    completed: int
    skipped: int
    failed: int
    results: tuple[Any, ...] = ()


class ScanCoordinator:
    """Process unique items with a hard concurrency bound.

    Runs on one coordinator are serialized. This closes the check/process/mark
    race between a manual scan and a scheduled scan while individual items still
    execute concurrently.
    """

    def __init__(
        self,
        *,
        processor: Callable[[Any], Awaitable[Any]],
        idempotency_store: IdempotencyStore,
        max_concurrency: int = 2,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        self._processor = processor
        self._idempotency = idempotency_store
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._run_lock = asyncio.Lock()

    @staticmethod
    def _key(item: object) -> str:
        value: object | None
        if isinstance(item, Mapping):
            value = item.get("idempotency_key")
        else:
            value = getattr(item, "idempotency_key", None)
        if not isinstance(value, str) or not value:
            raise ValueError("scan item requires a non-blank idempotency_key")
        return value

    async def run(self, items: Iterable[Any]) -> ScanResult:
        async with self._run_lock:
            unique: dict[str, Any] = {}
            skipped = 0
            for item in items:
                key = self._key(item)
                if key in unique:
                    skipped += 1
                    continue
                unique[key] = item

            pending: list[tuple[str, Any]] = []
            for key, item in unique.items():
                if await self._idempotency.was_completed(key):
                    skipped += 1
                else:
                    pending.append((key, item))

            async def process(key: str, item: Any) -> tuple[bool, Any]:
                async with self._semaphore:
                    try:
                        outcome = await self._processor(item)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        return False, error
                    await self._idempotency.mark_completed(key)
                    return True, outcome

            outcomes = await asyncio.gather(
                *(process(key, item) for key, item in pending)
            )
            completed_results = tuple(
                outcome for success, outcome in outcomes if success
            )
            failed = sum(not success for success, _ in outcomes)
            return ScanResult(
                completed=len(completed_results),
                skipped=skipped,
                failed=failed,
                results=completed_results,
            )
