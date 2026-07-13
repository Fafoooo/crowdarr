from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from backend.connectors.qbit import TorrentFile, TorrentSnapshot, find_stuck_nfos
from backend.core.repair import RepairStatus, TorrentRepairService

VIDEO_SIZE = 8_000_000_000


def make_snapshot(
    *,
    torrent_progress: float = 0.999,
    nfo_progress: float = 0.0,
    video_progress: float = 0.9999,
    state: str = "stalledDL",
    include_video: bool = True,
) -> TorrentSnapshot:
    files = [
        TorrentFile(
            index=7,
            path="Release.Name/Release.Name.nfo",
            size=42_000,
            progress=nfo_progress,
            priority=0,
        )
    ]
    if include_video:
        files.append(
            TorrentFile(
                index=8,
                path="Release.Name/Release.Name.mkv",
                size=VIDEO_SIZE,
                progress=video_progress,
                priority=1,
            )
        )
    return TorrentSnapshot(
        torrent_hash="deadbeef",
        name="Release.Name.2026-GROUP",
        category="cross-seed-link",
        content_path="/data/cross-seeds",
        progress=torrent_progress,
        state=state,
        files=files,
    )


def test_detects_incomplete_nfo_only_when_video_is_nearly_complete() -> None:
    missing = find_stuck_nfos(make_snapshot(), video_threshold=0.99)

    assert len(missing) == 1
    assert missing[0].torrent_hash == "deadbeef"
    assert missing[0].torrent_name == "Release.Name.2026-GROUP"
    assert missing[0].file_index == 7
    assert missing[0].relative_path == PurePosixPath("Release.Name/Release.Name.nfo")
    assert missing[0].reported_path == PurePosixPath(
        "/data/cross-seeds/Release.Name/Release.Name.nfo"
    )


def test_multifile_paths_follow_qbittorrent_actual_incomplete_content_path() -> None:
    torrent = TorrentSnapshot(
        torrent_hash="deadbeef",
        name="Release.Name.2026-GROUP",
        category="cross-seed-link",
        save_path="/data/completed",
        content_path="/data/incomplete/Release.Name",
        progress=0.999,
        state="stalledDL",
        files=[
            TorrentFile(
                index=7,
                path="Release.Name/Release.Name.nfo",
                size=42_000,
                progress=0.0,
                priority=0,
            ),
            TorrentFile(
                index=8,
                path="Release.Name/Release.Name.mkv",
                size=VIDEO_SIZE,
                progress=0.9999,
                priority=1,
            ),
        ],
    )

    missing = find_stuck_nfos(torrent)

    assert missing[0].reported_path == PurePosixPath(
        "/data/incomplete/Release.Name/Release.Name.nfo"
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        make_snapshot(video_progress=0.80),
        make_snapshot(nfo_progress=1.0),
        make_snapshot(torrent_progress=1.0, nfo_progress=1.0),
        make_snapshot(include_video=False),
    ],
    ids=["video-incomplete", "nfo-complete", "torrent-complete", "no-video"],
)
def test_stuck_detection_rejects_false_positives(snapshot: TorrentSnapshot) -> None:
    assert find_stuck_nfos(snapshot, video_threshold=0.99) == []


class FakeCrowdNFO:
    def __init__(self, payload: bytes, events: list[tuple[Any, ...]]) -> None:
        self.payload = payload
        self.events = events

    async def download_nfo(
        self, *, release_name: str, media_sha256: str | None = None
    ) -> bytes:
        self.events.append(("download", release_name, media_sha256))
        return self.payload


class FakePathMapper:
    def __init__(self, local_data: Path) -> None:
        self.local_data = local_data

    def map_path(self, reported_path: str | PurePosixPath) -> Path:
        reported = PurePosixPath(reported_path)
        assert reported.is_relative_to(PurePosixPath("/data"))
        return self.local_data.joinpath(*reported.relative_to("/data").parts)


class RecordingWriter:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events

    def __call__(self, path: Path, payload: bytes, *, overwrite: bool) -> bool:
        self.events.append(("write", path, payload, overwrite))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return True


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeQBit:
    def __init__(
        self,
        states: list[TorrentSnapshot],
        events: list[tuple[Any, ...]],
        *,
        resumed_state: TorrentSnapshot | None = None,
    ) -> None:
        self.states = states
        self.events = events
        self.resumed_state = resumed_state

    async def set_file_priority(
        self, torrent_hash: str, file_ids: list[int], priority: int
    ) -> None:
        self.events.append(("priority", torrent_hash, file_ids, priority))

    async def force_recheck(self, torrent_hash: str) -> None:
        self.events.append(("recheck", torrent_hash))

    async def get_torrent(self, torrent_hash: str) -> TorrentSnapshot:
        self.events.append(("poll", torrent_hash))
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    async def resume(self, torrent_hash: str) -> TorrentSnapshot | None:
        self.events.append(("resume", torrent_hash))
        return self.resumed_state


def checking_snapshot() -> TorrentSnapshot:
    return make_snapshot(state="checkingUP", nfo_progress=0.0)


def checked_snapshot(*, matched: bool) -> TorrentSnapshot:
    return make_snapshot(
        torrent_progress=1.0 if matched else 0.999,
        nfo_progress=1.0 if matched else 0.25,
        state="pausedUP",
    )


def seeding_snapshot() -> TorrentSnapshot:
    return make_snapshot(
        torrent_progress=1.0,
        nfo_progress=1.0,
        state="uploading",
    )


def make_service(
    *,
    local_data: Path,
    qbit: FakeQBit,
    crowdnfo: FakeCrowdNFO,
    writer: Callable[..., bool],
    clock: FakeClock,
    dry_run: bool = False,
    keep_mismatch: bool = True,
    recheck_timeout: float = 2.0,
) -> TorrentRepairService:
    return TorrentRepairService(
        crowdnfo=crowdnfo,
        qbit=qbit,
        path_mapper=FakePathMapper(local_data),
        atomic_writer=writer,
        allowed_roots=[local_data],
        poll_interval=1.0,
        recheck_timeout=recheck_timeout,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        dry_run=dry_run,
        keep_mismatch=keep_mismatch,
    )


@pytest.mark.asyncio
async def test_repair_writes_expected_path_then_rechecks_resumes_and_seeds(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    local_data = tmp_path / "data"
    media_path = local_data / "cross-seeds/Release.Name/Release.Name.mkv"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"never mutate this media")
    original_media = media_path.read_bytes()
    nfo_bytes = b"\xffExact\r\nNFO\r\n"
    qbit = FakeQBit(
        [checking_snapshot(), checked_snapshot(matched=True)],
        events,
        resumed_state=seeding_snapshot(),
    )
    service = make_service(
        local_data=local_data,
        qbit=qbit,
        crowdnfo=FakeCrowdNFO(nfo_bytes, events),
        writer=RecordingWriter(events),
        clock=FakeClock(),
    )
    candidate = find_stuck_nfos(make_snapshot())[0]

    result = await service.repair(candidate)

    expected_nfo = local_data / "cross-seeds/Release.Name/Release.Name.nfo"
    assert result.status is RepairStatus.SUCCESS
    assert result.verified is True
    assert result.seeding is True
    assert result.target_path == expected_nfo
    assert expected_nfo.read_bytes() == nfo_bytes
    assert media_path.read_bytes() == original_media
    assert [event[0] for event in events] == [
        "download",
        "write",
        "priority",
        "recheck",
        "poll",
        "poll",
        "resume",
    ]
    assert events[2] == ("priority", "deadbeef", [7], 1)


@pytest.mark.asyncio
async def test_repair_waits_for_recheck_to_start_before_judging_a_mismatch(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    qbit = FakeQBit(
        [
            make_snapshot(state="stalledDL"),
            checking_snapshot(),
            checked_snapshot(matched=True),
        ],
        events,
        resumed_state=seeding_snapshot(),
    )
    service = make_service(
        local_data=tmp_path / "data",
        qbit=qbit,
        crowdnfo=FakeCrowdNFO(b"exact nfo", events),
        writer=RecordingWriter(events),
        clock=clock,
        recheck_timeout=5.0,
    )

    result = await service.repair(find_stuck_nfos(make_snapshot())[0])

    assert result.status is RepairStatus.SUCCESS
    assert result.verified is True
    assert [event[0] for event in events].count("poll") == 3
    assert clock.value == 2.0


@pytest.mark.asyncio
async def test_repair_polls_after_resume_until_torrent_is_seeding(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    qbit = FakeQBit(
        [
            checking_snapshot(),
            checked_snapshot(matched=True),
            checked_snapshot(matched=True),
            seeding_snapshot(),
        ],
        events,
        resumed_state=None,
    )
    service = make_service(
        local_data=tmp_path / "data",
        qbit=qbit,
        crowdnfo=FakeCrowdNFO(b"exact nfo", events),
        writer=RecordingWriter(events),
        clock=clock,
        recheck_timeout=5.0,
    )

    result = await service.repair(find_stuck_nfos(make_snapshot())[0])

    assert result.status is RepairStatus.SUCCESS
    assert result.verified is True
    assert result.seeding is True
    assert [event[0] for event in events].count("poll") == 4
    assert clock.value >= 2.0


@pytest.mark.asyncio
async def test_queued_upload_is_not_reported_as_active_seeding(tmp_path: Path) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    queued = make_snapshot(
        torrent_progress=1.0,
        nfo_progress=1.0,
        state="queuedUP",
    )
    qbit = FakeQBit(
        [checking_snapshot(), checked_snapshot(matched=True), seeding_snapshot()],
        events,
        resumed_state=queued,
    )
    service = make_service(
        local_data=tmp_path / "data",
        qbit=qbit,
        crowdnfo=FakeCrowdNFO(b"exact nfo", events),
        writer=RecordingWriter(events),
        clock=clock,
        recheck_timeout=5.0,
    )

    result = await service.repair(find_stuck_nfos(make_snapshot())[0])

    assert result.status is RepairStatus.SUCCESS
    assert [event[0] for event in events].count("poll") == 3


@pytest.mark.asyncio
async def test_multiple_missing_nfos_are_written_before_one_recheck(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    first = TorrentFile(7, "Release.Name/first.nfo", 100, 0.0, 0)
    second = TorrentFile(9, "Release.Name/second.nfo", 100, 0.0, 0)
    video = TorrentFile(
        8,
        "Release.Name/Release.Name.mkv",
        VIDEO_SIZE,
        1.0,
        1,
    )
    stuck = replace(make_snapshot(), files=[first, second, video])
    checking = replace(stuck, state="checkingUP")
    verified_files = [
        replace(first, progress=1.0),
        replace(second, progress=1.0),
        video,
    ]
    verified = replace(
        stuck,
        progress=1.0,
        state="stoppedUP",
        files=verified_files,
    )
    seeding = replace(verified, state="uploading")
    qbit = FakeQBit([checking, verified], events, resumed_state=seeding)
    service = make_service(
        local_data=tmp_path / "data",
        qbit=qbit,
        crowdnfo=FakeCrowdNFO(b"exact nfo", events),
        writer=RecordingWriter(events),
        clock=FakeClock(),
    )

    results = await service.repair_many(find_stuck_nfos(stuck))

    assert [result.status for result in results] == [
        RepairStatus.SUCCESS,
        RepairStatus.SUCCESS,
    ]
    assert [event[0] for event in events].count("recheck") == 1
    assert [event[0] for event in events].count("resume") == 1
    assert ("priority", "deadbeef", [7, 9], 1) in events


@pytest.mark.asyncio
async def test_repair_only_reports_seeding_timeout_after_polling_to_deadline(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    qbit = FakeQBit(
        [
            checking_snapshot(),
            checked_snapshot(matched=True),
            checked_snapshot(matched=True),
        ],
        events,
        resumed_state=None,
    )
    service = make_service(
        local_data=tmp_path / "data",
        qbit=qbit,
        crowdnfo=FakeCrowdNFO(b"exact nfo", events),
        writer=RecordingWriter(events),
        clock=clock,
        recheck_timeout=3.0,
    )

    result = await service.repair(find_stuck_nfos(make_snapshot())[0])

    assert result.status is RepairStatus.TIMEOUT
    assert result.verified is True
    assert result.seeding is False
    assert result.retryable is True
    assert clock.value >= 3.0
    assert [event[0] for event in events].count("poll") >= 5


@pytest.mark.asyncio
async def test_disabled_auto_recheck_places_nfo_without_claiming_verified_success(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    local_data = tmp_path / "data"
    target = local_data / "cross-seeds/Release.Name/Release.Name.nfo"
    clock = FakeClock()
    service = TorrentRepairService(
        crowdnfo=FakeCrowdNFO(b"exact nfo", events),
        qbit=FakeQBit([], events),
        path_mapper=FakePathMapper(local_data),
        atomic_writer=RecordingWriter(events),
        poll_interval=1.0,
        recheck_timeout=3.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        auto_recheck=False,
    )

    result = await service.repair(find_stuck_nfos(make_snapshot())[0])

    assert target.read_bytes() == b"exact nfo"
    assert result.status is not RepairStatus.SUCCESS
    assert result.verified is False
    assert result.seeding is False
    assert "recheck" not in [event[0] for event in events]
    assert "poll" not in [event[0] for event in events]
    assert "resume" not in [event[0] for event in events]


@pytest.mark.asyncio
async def test_repair_reports_mismatch_and_removes_only_downloaded_nfo(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    local_data = tmp_path / "data"
    media_path = local_data / "cross-seeds/Release.Name/Release.Name.mkv"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"media stays intact")
    qbit = FakeQBit([checking_snapshot(), checked_snapshot(matched=False)], events)
    service = make_service(
        local_data=local_data,
        qbit=qbit,
        crowdnfo=FakeCrowdNFO(b"wrong bytes", events),
        writer=RecordingWriter(events),
        clock=FakeClock(),
        keep_mismatch=False,
    )

    result = await service.repair(find_stuck_nfos(make_snapshot())[0])

    expected_nfo = local_data / "cross-seeds/Release.Name/Release.Name.nfo"
    assert result.status is RepairStatus.MISMATCH
    assert result.retryable is True
    assert "nfo mismatch" in result.message.lower()
    assert not expected_nfo.exists()
    assert media_path.read_bytes() == b"media stays intact"
    assert "resume" not in [event[0] for event in events]


@pytest.mark.asyncio
async def test_mismatch_cleanup_preserves_nfo_replaced_during_recheck(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    local_data = tmp_path / "data"
    target = local_data / "cross-seeds/Release.Name/Release.Name.nfo"
    target.parent.mkdir(parents=True)
    replacement = b"replacement written by another process"

    class ReplacingQBit(FakeQBit):
        async def get_torrent(self, torrent_hash: str) -> TorrentSnapshot:
            snapshot = await super().get_torrent(torrent_hash)
            if snapshot.state == "pausedUP":
                target.write_bytes(replacement)
            return snapshot

    clock = FakeClock()
    service = TorrentRepairService(
        crowdnfo=FakeCrowdNFO(b"mismatched downloaded bytes", events),
        qbit=ReplacingQBit(
            [checking_snapshot(), checked_snapshot(matched=False)], events
        ),
        path_mapper=FakePathMapper(local_data),
        allowed_roots=[local_data],
        poll_interval=1.0,
        recheck_timeout=2.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        keep_mismatch=False,
    )

    result = await service.repair(find_stuck_nfos(make_snapshot())[0])

    assert result.status is RepairStatus.MISMATCH
    assert target.read_bytes() == replacement


@pytest.mark.asyncio
async def test_repair_timeout_is_retryable_and_never_touches_media(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    local_data = tmp_path / "data"
    media_path = local_data / "cross-seeds/Release.Name/Release.Name.mkv"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"untouched")
    qbit = FakeQBit([checking_snapshot()], events)
    service = make_service(
        local_data=local_data,
        qbit=qbit,
        crowdnfo=FakeCrowdNFO(b"candidate nfo", events),
        writer=RecordingWriter(events),
        clock=FakeClock(),
    )

    result = await service.repair(find_stuck_nfos(make_snapshot())[0])

    assert result.status is RepairStatus.TIMEOUT
    assert result.retryable is True
    assert media_path.read_bytes() == b"untouched"
    assert "resume" not in [event[0] for event in events]


@pytest.mark.asyncio
async def test_dry_run_reports_mapped_target_without_download_write_or_qbit_mutation(
    tmp_path: Path,
) -> None:
    events: list[tuple[Any, ...]] = []
    local_data = tmp_path / "data"
    service = make_service(
        local_data=local_data,
        qbit=FakeQBit([checking_snapshot()], events),
        crowdnfo=FakeCrowdNFO(b"unused", events),
        writer=RecordingWriter(events),
        clock=FakeClock(),
        dry_run=True,
    )

    result = await service.repair(find_stuck_nfos(make_snapshot())[0])

    assert result.status is RepairStatus.DRY_RUN
    assert result.target_path == (
        local_data / "cross-seeds/Release.Name/Release.Name.nfo"
    )
    assert events == []
    assert not result.target_path.exists()
