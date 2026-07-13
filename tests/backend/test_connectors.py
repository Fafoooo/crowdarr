from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from backend.connectors.health import ConnectorSupervisor
from backend.connectors.qbit import QBitConnector
from backend.connectors.radarr import RadarrConnector
from backend.connectors.sab import SABCompletionEvent, SABWebhookHandler, SABnzbdConnector
from backend.connectors.sonarr import SonarrConnector
from backend.connectors.umlaut import UmlautAdaptarrConnector
from backend.core.contribution import ContributionItem, ContributionService
from backend.core.files import PathMapper, PathMapping
from backend.core.library import find_missing_sidecars
from backend.core.mediainfo import MediaInfoRunner


@pytest.mark.asyncio
async def test_qbit_authenticated_webui_request_shapes_and_health(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v2/auth/login":
            assert httpx.QueryParams(request.content.decode()) == httpx.QueryParams(
                {"username": "alice", "password": "secret"}
            )
            return httpx.Response(
                200,
                text="Ok.",
                headers={"Set-Cookie": "SID=test-session; HttpOnly; Path=/"},
            )
        if path == "/api/v2/torrents/info":
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "deadbeef",
                        "name": "Release-GROUP",
                        "category": "cross-seed-link",
                        "content_path": "/data/cross-seeds/Release-GROUP",
                        "progress": 0.999,
                        "state": "stalledDL",
                    }
                ],
            )
        if path == "/api/v2/torrents/files":
            assert request.url.params == httpx.QueryParams({"hash": "deadbeef"})
            return httpx.Response(
                200,
                json=[
                    {
                        "index": 4,
                        "name": "Release-GROUP.nfo",
                        "size": 8192,
                        "progress": 0.0,
                        "priority": 0,
                    }
                ],
            )
        if path in {
            "/api/v2/torrents/filePrio",
            "/api/v2/torrents/recheck",
            "/api/v2/torrents/resume",
        }:
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/app/version":
            return httpx.Response(200, text="v5.0.4")
        raise AssertionError(f"unexpected qBittorrent request: {request}")

    data_root = tmp_path / "data"
    data_root.mkdir()
    mapper = PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=data_root)],
        allowed_roots=[data_root],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        connector = QBitConnector(
            base_url="http://qbittorrent:8080",
            username="alice",
            password="secret",
            http_client=http,
            path_mapper=mapper,
        )
        torrents = await connector.list_torrents()
        files = await connector.list_files("deadbeef")
        await connector.set_file_priority("deadbeef", [4], priority=1)
        await connector.force_recheck("deadbeef")
        await connector.resume("deadbeef")
        health = await connector.healthcheck()

    assert torrents[0].torrent_hash == "deadbeef"
    assert torrents[0].local_content_path == data_root / "cross-seeds/Release-GROUP"
    assert files[0].index == 4 and files[0].path == "Release-GROUP.nfo"
    assert health.healthy is True and health.version == "v5.0.4"
    assert [request.url.path for request in requests] == [
        "/api/v2/auth/login",
        "/api/v2/torrents/info",
        "/api/v2/torrents/files",
        "/api/v2/torrents/filePrio",
        "/api/v2/torrents/recheck",
        "/api/v2/torrents/resume",
        "/api/v2/app/version",
    ]
    assert httpx.QueryParams(requests[3].content.decode()) == httpx.QueryParams(
        {"hash": "deadbeef", "id": "4", "priority": "1"}
    )
    assert httpx.QueryParams(requests[4].content.decode()) == httpx.QueryParams(
        {"hashes": "deadbeef"}
    )
    assert httpx.QueryParams(requests[5].content.decode()) == httpx.QueryParams(
        {"hashes": "deadbeef"}
    )
    assert all(
        request.headers.get("Cookie") == "SID=test-session"
        for request in requests[1:]
    )


@pytest.mark.asyncio
async def test_qbit_can_use_webui_auth_whitelist_without_login() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[])
        return httpx.Response(200, text="v5.0.4")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        connector = QBitConnector(
            base_url="http://qbittorrent:8080",
            username=None,
            password=None,
            http_client=http,
        )
        assert await connector.list_torrents() == []
        assert (await connector.healthcheck()).healthy is True

    assert "/api/v2/auth/login" not in paths


class FakeLiveService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, SABCompletionEvent]] = []

    async def fetch_missing(self, event: SABCompletionEvent) -> None:
        self.calls.append(("fetch", event))

    async def contribute(self, event: SABCompletionEvent) -> None:
        self.calls.append(("contribute", event))


@pytest.mark.asyncio
async def test_sab_completion_api_and_webhook_can_fetch_and_contribute() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "history": {
                    "slots": [
                        {
                            "name": "Movie.2026-GROUP",
                            "status": "Completed",
                            "storage": "/data/downloads/movies/Movie.2026-GROUP",
                            "category": "movies",
                            "nzo_id": "SABnzbd_nzo_1",
                        },
                        {"name": "Failed", "status": "Failed"},
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sab = SABnzbdConnector(
            base_url="http://sabnzbd:8080",
            api_key="sab-key",
            http_client=http,
        )
        completed = await sab.list_completed()

    assert len(completed) == 1
    assert completed[0].release_name == "Movie.2026-GROUP"
    assert captured[0].url.params["mode"] == "history"
    assert captured[0].url.params["output"] == "json"
    assert captured[0].url.params["apikey"] == "sab-key"

    live = FakeLiveService()
    webhook = SABWebhookHandler(
        live_service=live,
        fetch_enabled=True,
        contribute_enabled=True,
    )
    result = await webhook.handle(completed[0])
    assert result.accepted is True
    assert [call[0] for call in live.calls] == ["fetch", "contribute"]


@pytest.mark.asyncio
async def test_radarr_sonarr_and_umlaut_enumerate_original_release_sidecars(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    movie_path = data_root / "movies/Movie/Movie.mkv"
    episode_path = data_root / "series/Show/Season 01/Show.S01E01.mkv"
    movie_path.parent.mkdir(parents=True)
    episode_path.parent.mkdir(parents=True)
    movie_path.write_bytes(b"movie")
    episode_path.write_bytes(b"episode")
    mapper = PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=data_root)],
        allowed_roots=[data_root],
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "radarr" and request.url.path == "/api/v3/movie":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "title": "Renamed Movie",
                        "path": "/data/movies/Movie",
                        "hasFile": True,
                        "movieFile": {
                            "path": "/data/movies/Movie/Movie.mkv",
                            "sceneName": "Movie.2026-GROUP",
                        },
                    }
                ],
            )
        if request.url.host == "sonarr" and request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 2, "title": "Renamed Show"}])
        if request.url.host == "sonarr" and request.url.path == "/api/v3/episodefile":
            assert request.url.params == httpx.QueryParams({"seriesId": "2"})
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 3,
                        "path": "/data/series/Show/Season 01/Show.S01E01.mkv",
                        "sceneName": "Show.S01E01-GROUP",
                    }
                ],
            )
        if request.url.host == "umlaut" and request.url.path == "/titlelookup":
            assert request.url.params == httpx.QueryParams(
                {"changedTitle": "Renamed.Release"}
            )
            return httpx.Response(200, json={"originalTitle": "Original.Release-GROUP"})
        raise AssertionError(f"unexpected connector request: {request}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        radarr = RadarrConnector(
            base_url="http://radarr:7878",
            api_key="radarr-key",
            http_client=http,
            path_mapper=mapper,
        )
        sonarr = SonarrConnector(
            base_url="http://sonarr:8989",
            api_key="sonarr-key",
            http_client=http,
            path_mapper=mapper,
        )
        umlaut = UmlautAdaptarrConnector(
            base_url="http://umlaut:8080",
            http_client=http,
        )
        items = [*(await radarr.list_media()), *(await sonarr.list_media())]
        recovered = await umlaut.recover_release_name("Renamed.Release")

    assert [item.release_name for item in items] == [
        "Movie.2026-GROUP",
        "Show.S01E01-GROUP",
    ]
    assert [item.local_media_path for item in items] == [movie_path, episode_path]
    missing = find_missing_sidecars(items)
    assert [item.sidecar_path for item in missing] == [
        movie_path.with_suffix(".nfo"),
        episode_path.with_suffix(".nfo"),
    ]
    assert recovered == "Original.Release-GROUP"
    assert all(
        request.headers["X-Api-Key"] == "radarr-key"
        for request in requests
        if request.url.host == "radarr"
    )
    assert all(
        request.headers["X-Api-Key"] == "sonarr-key"
        for request in requests
        if request.url.host == "sonarr"
    )


class BrokenConnector:
    async def scan(self) -> list[Any]:
        raise ConnectionError("offline; api_key=secret-must-not-leak")


class HealthyConnector:
    async def scan(self) -> list[str]:
        return ["one-item"]


@pytest.mark.asyncio
async def test_down_connector_is_skipped_logged_and_does_not_abort_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    supervisor = ConnectorSupervisor()

    results = await supervisor.run_all(
        operation="scan",
        connectors={"radarr": BrokenConnector(), "sonarr": HealthyConnector()},
    )

    assert results["radarr"].skipped is True
    assert results["radarr"].error == "connection failed"
    assert results["sonarr"].value == ["one-item"]
    assert results["sonarr"].skipped is False
    assert "radarr" in caplog.text and "unavailable" in caplog.text
    assert "secret-must-not-leak" not in caplog.text


class FakeCommandRunner:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *command: str) -> SimpleNamespace:
        self.calls.append(command)
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr=b"")


@pytest.mark.asyncio
async def test_mediainfo_invocation_returns_subprocess_stdout_as_raw_bytes(
    tmp_path: Path,
) -> None:
    media = tmp_path / "release.mkv"
    media.write_bytes(b"media")
    raw_output = b'{"media":{"track":[]},"marker":"\xff"}\r\n'
    command_runner = FakeCommandRunner(raw_output)
    runner = MediaInfoRunner(executable="mediainfo", command_runner=command_runner)

    result = await runner.inspect(media)

    assert result == raw_output
    assert command_runner.calls == [
        ("mediainfo", "--Output=JSON", str(media))
    ]


class PartialFailureCrowdNFO:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def upload_nfo(self, **kwargs: Any) -> None:
        self.calls.append(("nfo", kwargs))
        raise httpx.ConnectError("nfo upload unavailable")

    async def upload_mediainfo(self, **kwargs: Any) -> None:
        self.calls.append(("mediainfo", kwargs))

    async def upload_filelist(self, **kwargs: Any) -> None:
        self.calls.append(("filelist", kwargs))


class FakeMediaInfo:
    async def inspect(self, media_path: Path) -> bytes:
        return b"raw mediainfo bytes"


@pytest.mark.asyncio
async def test_contribution_components_are_independent_and_category_is_mapped(
    tmp_path: Path,
) -> None:
    media = tmp_path / "Movie.mkv"
    nfo = tmp_path / "Movie.nfo"
    media.write_bytes(b"media")
    nfo.write_bytes(b"nfo")
    crowdnfo = PartialFailureCrowdNFO()
    service = ContributionService(
        crowdnfo=crowdnfo,
        mediainfo=FakeMediaInfo(),
        category_mapping={"radarr": "Movies", "sonarr": "TV"},
    )
    item = ContributionItem(
        release_name="Movie.2026-GROUP",
        media_path=media,
        nfo_path=nfo,
        source_category="radarr",
        media_sha256="ab" * 32,
        filelist=[{"file_path": "Movie.mkv", "file_size_bytes": 5}],
    )

    result = await service.contribute(
        item,
        include_nfo=True,
        include_mediainfo=True,
        include_filelist=True,
    )

    assert [name for name, _ in crowdnfo.calls] == [
        "nfo",
        "mediainfo",
        "filelist",
    ]
    assert all(call[1]["category"] == "Movies" for call in crowdnfo.calls)
    assert crowdnfo.calls[1][1]["content"] == b"raw mediainfo bytes"
    assert result.status == "partial"
    assert result.components["nfo"].status == "failed"
    assert result.components["mediainfo"].status == "success"
    assert result.components["filelist"].status == "success"
