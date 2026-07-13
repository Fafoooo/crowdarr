from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from backend.connectors.health import (
    ConnectorSupervisor,
    normalize_base_url,
    response_records,
    sanitized_error,
    secret_value,
)
from backend.connectors.radarr import RadarrConnector
from backend.connectors.sab import (
    SABCompletionEvent,
    SABLiveActionResult,
    SABnzbdConnector,
    SABWebhookHandler,
)
from backend.connectors.sonarr import SonarrConnector
from backend.connectors.umlaut import UmlautAdaptarrConnector
from backend.core.contribution import ContributionItem, ContributionService
from backend.core.files import PathMapper, PathMapping
from backend.core.mediainfo import (
    AsyncSubprocessRunner,
    MediaInfoError,
    MediaInfoRunner,
)
from backend.crowdnfo.client import CrowdNFOClient


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://service.invalid/status")
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        return error
    raise AssertionError("expected an HTTP status error")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("secret endpoint"), "connection failed"),
        (_http_status_error(401), "authentication failed"),
        (_http_status_error(429), "rate limited"),
        (_http_status_error(503), "service unavailable"),
        (_http_status_error(422), "request failed"),
        (PermissionError("private path"), "permission denied"),
        (FileNotFoundError("private path"), "file unavailable"),
        (ValueError("private value"), "invalid input"),
        (RuntimeError("private detail"), "operation failed"),
    ],
)
def test_connector_errors_are_classified_without_exposing_details(
    error: BaseException,
    expected: str,
) -> None:
    assert sanitized_error(error) == expected


class _Secret:
    def get_secret_value(self) -> str:
        return "decrypted"


def test_connector_helpers_normalize_secrets_urls_and_paginated_records() -> None:
    assert secret_value(None) is None
    assert secret_value(123) == "123"
    assert secret_value(_Secret()) == "decrypted"
    assert normalize_base_url("https://example.test/api/", service="Example") == (
        "https://example.test/api"
    )
    assert response_records(
        {"records": [{"id": 1}, "ignored", {"id": 2}]}, service="Example"
    ) == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize(
    "url",
    [
        "not a url",
        "ftp://example.test",
        "https://user:pass@example.test",
        "https://example.test?api_key=secret",
        "https://example.test/#fragment",
    ],
)
def test_connector_base_urls_reject_ambiguous_or_secret_bearing_urls(url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        normalize_base_url(url, service="Example")


@pytest.mark.parametrize("payload", [{"items": []}, "not-a-list", None])
def test_connector_record_parser_rejects_unexpected_shapes(payload: object) -> None:
    with pytest.raises(ValueError, match="contain a list"):
        response_records(payload, service="Example")


class _UnsupportedConnector:
    pass


class _SynchronousConnector:
    def scan(self) -> list[str]:
        return ["not awaitable"]


@pytest.mark.asyncio
async def test_connector_supervisor_skips_unsupported_and_synchronous_connectors() -> (
    None
):
    outcomes = await ConnectorSupervisor().run_all(
        operation="scan",
        connectors={
            "unsupported\x00name": _UnsupportedConnector(),
            "sync": _SynchronousConnector(),
        },
    )

    assert outcomes["unsupported\x00name"].error == "operation unsupported"
    assert outcomes["sync"].error == "operation failed"
    assert all(outcome.skipped for outcome in outcomes.values())


@pytest.mark.asyncio
async def test_connector_supervisor_rejects_private_or_invalid_operations() -> None:
    supervisor = ConnectorSupervisor()
    for operation in ("_scan", "not-public-method"):
        with pytest.raises(ValueError, match="public connector method"):
            await supervisor.run_all(operation=operation, connectors={})


def _mapper(tmp_path: Path) -> PathMapper:
    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    return PathMapper(
        mappings=[PathMapping(remote_root="/data", local_root=data_root)],
        allowed_roots=[data_root],
    )


@pytest.mark.asyncio
async def test_radarr_accepts_paginated_relative_paths_and_skips_unsafe_records(
    tmp_path: Path,
) -> None:
    mapper = _mapper(tmp_path)
    payload = {
        "records": [
            {"id": 1, "hasFile": False},
            {"id": 2, "hasFile": True, "movieFile": "invalid"},
            {
                "id": 3,
                "path": "/data/movies/Good",
                "movieFile": {
                    "id": {"unexpected": "id"},
                    "relativePath": "Good.Movie.mkv",
                    "sceneName": None,
                },
            },
            {
                "id": 4,
                "path": "/data/movies/Escape",
                "movieFile": {"relativePath": "../Escape.mkv"},
            },
            {
                "id": 5,
                "movieFile": {"path": "/unmapped/Movie.mkv"},
            },
            {
                "id": 6,
                "movieFile": {
                    "path": "/data/movies/Unsafe?.mkv",
                    "sceneName": "unsafe/name",
                },
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "decrypted"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        connector = RadarrConnector(
            base_url="http://radarr:7878/",
            api_key=_Secret(),
            http_client=http,
            path_mapper=mapper,
        )
        records = await connector.scan()

    assert len(records) == 1
    assert records[0].release_name == "Good.Movie"
    assert records[0].local_media_path == tmp_path / "data/movies/Good/Good.Movie.mkv"
    assert records[0].item_id is None


@pytest.mark.asyncio
async def test_radarr_health_reports_bounded_version_and_sanitized_failure(
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            httpx.Response(200, json={"version": "v" * 150}),
            httpx.Response(403, text="api_key=must-not-leak"),
        ]
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: next(responses))
    ) as http:
        connector = RadarrConnector(
            base_url="http://radarr:7878",
            api_key="key",
            http_client=http,
            path_mapper=_mapper(tmp_path),
        )
        healthy = await connector.healthcheck()
        unhealthy = await connector.healthcheck()

    assert healthy.healthy is True
    assert healthy.version == "v" * 100
    assert unhealthy.healthy is False
    assert unhealthy.detail == "authentication failed"


@pytest.mark.parametrize("connector", [RadarrConnector, SonarrConnector])
def test_arr_connectors_require_an_api_key(
    connector: type[RadarrConnector] | type[SonarrConnector], tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="api_key is required"):
        connector(
            base_url="http://arr:8080",
            api_key="",
            path_mapper=_mapper(tmp_path),
        )


@pytest.mark.asyncio
async def test_sonarr_skips_invalid_series_files_and_falls_back_to_filename(
    tmp_path: Path,
) -> None:
    mapper = _mapper(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/series":
            return httpx.Response(
                200,
                json=[{"id": None}, {"id": 7}, {"id": "series-eight"}],
            )
        assert request.url.path == "/api/v3/episodefile"
        if request.url.params["seriesId"] == "7":
            return httpx.Response(
                200,
                json={
                    "records": [
                        {"path": None},
                        {
                            "id": {"bad": "id"},
                            "path": "/data/series/Show/Show.S01E01.mkv",
                            "sceneName": None,
                        },
                        {"path": "/unmapped/Show.S01E02.mkv"},
                        {
                            "path": "/data/series/Show/Unsafe?.mkv",
                            "sceneName": "unsafe/name",
                        },
                    ]
                },
            )
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        connector = SonarrConnector(
            base_url="http://sonarr:8989",
            api_key="key",
            http_client=http,
            path_mapper=mapper,
        )
        records = await connector.scan()

    assert len(records) == 1
    assert records[0].release_name == "Show.S01E01"
    assert records[0].item_id is None


@pytest.mark.asyncio
async def test_sonarr_health_handles_non_object_success_and_server_failure(
    tmp_path: Path,
) -> None:
    responses = iter(
        [httpx.Response(200, json=[]), httpx.Response(503, text="private")]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: next(responses))
    ) as http:
        connector = SonarrConnector(
            base_url="http://sonarr:8989",
            api_key="key",
            http_client=http,
            path_mapper=_mapper(tmp_path),
        )
        healthy = await connector.healthcheck()
        unhealthy = await connector.healthcheck()

    assert healthy.healthy is True and healthy.version is None
    assert unhealthy.detail == "service unavailable"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"release_name": "unsafe/name", "storage_path": "/data/release"},
        {"release_name": "Release", "storage_path": ""},
        {"release_name": "Release", "storage_path": "/data/line\nbreak"},
    ],
)
def test_sab_completion_event_rejects_unsafe_boundary_values(
    kwargs: Mapping[str, str],
) -> None:
    with pytest.raises(ValueError, match="unsafe SABnzbd"):
        SABCompletionEvent(**kwargs)


def test_sab_completion_event_normalizes_optional_metadata() -> None:
    event = SABCompletionEvent(
        release_name="  Release-GROUP  ",
        storage_path="  /data/Release-GROUP  ",
        category=" " * 101,
        nzo_id="bad\nidentifier",
    )

    assert event.release_name == "Release-GROUP"
    assert event.remote_storage_path == "/data/Release-GROUP"
    assert event.category == ""
    assert event.nzo_id is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must be an object"),
        ({}, "missing history"),
        ({"history": {"slots": {}}}, "slots must be a list"),
    ],
)
@pytest.mark.asyncio
async def test_sab_history_rejects_invalid_response_shapes(
    payload: object,
    message: str,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=payload)
        )
    ) as http:
        connector = SABnzbdConnector(
            base_url="http://sab:8080",
            api_key=None,
            http_client=http,
        )
        with pytest.raises(ValueError, match=message):
            await connector.list_completed()


@pytest.mark.asyncio
async def test_sab_history_skips_malformed_completed_entries_without_api_key() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "history": {
                    "slots": [
                        "invalid",
                        {"status": "Failed"},
                        {"status": "Completed", "name": "unsafe/name"},
                        {
                            "status": "completed",
                            "name": "Valid-GROUP",
                            "storage": "/data/Valid-GROUP",
                            "category": 12,
                            "nzo_id": " " * 201,
                        },
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        connector = SABnzbdConnector(
            base_url="http://sab:8080",
            api_key=None,
            http_client=http,
        )
        completed = await connector.scan()

    assert completed == [SABCompletionEvent("Valid-GROUP", "/data/Valid-GROUP")]
    assert "apikey" not in captured[0].url.params


@pytest.mark.asyncio
async def test_sab_health_tolerates_non_json_success_and_sanitizes_failure() -> None:
    responses = iter(
        [
            httpx.Response(200, content=b"not-json"),
            httpx.Response(401, text="apikey=must-not-leak"),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: next(responses))
    ) as http:
        connector = SABnzbdConnector(
            base_url="http://sab:8080",
            api_key="key",
            http_client=http,
        )
        healthy = await connector.healthcheck()
        unhealthy = await connector.healthcheck()

    assert healthy.healthy is True and healthy.version is None
    assert unhealthy.healthy is False
    assert unhealthy.detail == "authentication failed"


class _FailingLiveService:
    async def fetch_missing(self, _event: SABCompletionEvent) -> None:
        raise httpx.ConnectError("api_key=must-not-leak")

    async def contribute(self, _event: SABCompletionEvent) -> None:
        raise PermissionError("private directory")


@pytest.mark.asyncio
async def test_sab_webhook_runs_enabled_steps_independently_and_sanitizes_errors() -> (
    None
):
    handler = SABWebhookHandler(
        live_service=_FailingLiveService(),
        fetch_enabled=True,
        contribute_enabled=True,
    )

    result = await handler.handle(SABCompletionEvent("Release", "/data/Release"))

    assert result.accepted is True
    assert result.actions == ("fetch", "contribute")
    assert result.errors == {
        "fetch": "connection failed",
        "contribute": "permission denied",
    }


@pytest.mark.asyncio
async def test_sab_webhook_reports_no_actions_when_both_modes_are_disabled() -> None:
    handler = SABWebhookHandler(
        live_service=_FailingLiveService(),
        fetch_enabled=False,
        contribute_enabled=False,
    )

    result = await handler.handle(SABCompletionEvent("Release", "/data/Release"))

    assert result.actions == () and result.errors == {}


class _OutcomeLiveService:
    async def fetch_missing(self, _event: SABCompletionEvent) -> SABLiveActionResult:
        return SABLiveActionResult(performed=False, value=Path("existing.nfo"))

    async def contribute(self, _event: SABCompletionEvent) -> SABLiveActionResult:
        return SABLiveActionResult(
            performed=True,
            value=object(),
            warning="MediaInfo upload failed",
        )


@pytest.mark.asyncio
async def test_sab_webhook_distinguishes_noop_from_performed_actions() -> None:
    handler = SABWebhookHandler(
        live_service=_OutcomeLiveService(),
        fetch_enabled=True,
        contribute_enabled=True,
    )

    result = await handler.handle(SABCompletionEvent("Release", "/data/Release"))

    assert result.actions == ("fetch", "contribute")
    assert result.performed_actions == ("contribute",)
    assert result.warnings == {"contribute": "MediaInfo upload failed"}


@pytest.mark.asyncio
async def test_umlaut_lookup_handles_miss_invalid_payload_and_unsafe_names() -> None:
    responses = iter(
        [
            httpx.Response(404),
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"originalTitle": "unsafe/name"}),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: next(responses))
    ) as http:
        connector = UmlautAdaptarrConnector(
            base_url="http://umlaut:8080", http_client=http
        )
        assert await connector.recover_release_name("Missing.Release") is None
        with pytest.raises(ValueError, match="must be an object"):
            await connector.recover_release_name("Bad.Response")
        assert await connector.recover_release_name("Unsafe.Response") is None
        with pytest.raises(ValueError, match="changed title is unsafe"):
            await connector.recover_release_name("unsafe/title")


@pytest.mark.asyncio
async def test_umlaut_health_accepts_not_found_and_sanitizes_outages() -> None:
    responses = iter([httpx.Response(404), httpx.Response(503, text="private")])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: next(responses))
    ) as http:
        connector = UmlautAdaptarrConnector(
            base_url="http://umlaut:8080", http_client=http
        )
        healthy = await connector.healthcheck()
        unhealthy = await connector.healthcheck()

    assert healthy.healthy is True
    assert unhealthy.healthy is False
    assert unhealthy.detail == "service unavailable"


@pytest.mark.parametrize(
    ("executable", "timeout", "message"),
    [
        ("", 1.0, "cannot be blank"),
        ("bad\x00binary", 1.0, "cannot be blank"),
        ("mediainfo", 0.0, "must be positive"),
    ],
)
def test_mediainfo_configuration_rejects_unsafe_values(
    executable: str,
    timeout: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        MediaInfoRunner(executable=executable, timeout=timeout)


class _CommandRunner:
    def __init__(
        self,
        result: object | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._result = result
        self._error = error

    async def run(self, *_command: str) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


@pytest.mark.asyncio
async def test_mediainfo_reports_missing_media_and_process_failures(
    tmp_path: Path,
) -> None:
    missing = MediaInfoRunner(command_runner=_CommandRunner())
    with pytest.raises(MediaInfoError, match="media file unavailable"):
        await missing.inspect(tmp_path / "missing.mkv")

    media = tmp_path / "release.mkv"
    media.write_bytes(b"media")
    cases = [
        (_CommandRunner(error=FileNotFoundError()), "executable unavailable"),
        (_CommandRunner(error=TimeoutError()), "timed out"),
        (
            _CommandRunner(SimpleNamespace(returncode=3, stdout=b"", stderr=b"bad")),
            "exited unsuccessfully",
        ),
        (
            _CommandRunner(SimpleNamespace(returncode=0, stdout="text", stderr=b"")),
            "non-byte output",
        ),
    ]
    for command_runner, message in cases:
        runner = MediaInfoRunner(command_runner=command_runner)
        with pytest.raises(MediaInfoError, match=message):
            await runner.inspect(media)


@pytest.mark.asyncio
async def test_async_subprocess_runner_returns_stdout_stderr_and_exit_code() -> None:
    result = await AsyncSubprocessRunner().run(
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'raw'); "
        "sys.stderr.buffer.write(b'warning'); raise SystemExit(4)",
    )

    assert result.returncode == 4
    assert result.stdout == b"raw"
    assert result.stderr == b"warning"


class _RecordingUploader:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def upload_nfo(self, **kwargs: Any) -> None:
        self.calls.append(("nfo", kwargs))
        if self.fail:
            raise ValueError("private invalid value")

    async def upload_mediainfo(self, **kwargs: Any) -> None:
        self.calls.append(("mediainfo", kwargs))
        if self.fail:
            raise ValueError("private invalid value")

    async def upload_filelist(self, **kwargs: Any) -> None:
        self.calls.append(("filelist", kwargs))
        if self.fail:
            raise ValueError("private invalid value")


class _StaticMediaInfo:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def inspect(self, _media_path: Path) -> bytes:
        return self.content


@pytest.mark.parametrize(
    "kwargs",
    [
        {"release_name": "unsafe/name", "media_path": Path("movie.mkv")},
        {
            "release_name": "Release",
            "media_path": Path("movie.mkv"),
            "media_sha256": "not-a-digest",
        },
    ],
)
def test_contribution_item_rejects_unsafe_release_metadata(
    kwargs: Mapping[str, Any],
) -> None:
    with pytest.raises(ValueError):
        ContributionItem(**kwargs)


def test_contribution_service_rejects_invalid_category_mapping() -> None:
    with pytest.raises(ValueError, match="invalid category"):
        ContributionService(
            crowdnfo=_RecordingUploader(),
            mediainfo=_StaticMediaInfo(b"json"),
            category_mapping={"radarr": "NotACrowdNFOCategory"},
        )


@pytest.mark.asyncio
async def test_contribution_returns_skipped_when_no_components_are_enabled(
    tmp_path: Path,
) -> None:
    service = ContributionService(
        crowdnfo=_RecordingUploader(), mediainfo=_StaticMediaInfo(b"json")
    )
    result = await service.contribute(
        ContributionItem("Release", tmp_path / "movie.mkv"),
        include_nfo=False,
        include_mediainfo=False,
        include_filelist=False,
    )

    assert result.status == "skipped"
    assert all(
        component.status == "skipped" for component in result.components.values()
    )


@pytest.mark.asyncio
async def test_contribution_normalizes_filelist_and_reports_complete_success(
    tmp_path: Path,
) -> None:
    media = tmp_path / "Movie.mkv"
    nfo = tmp_path / "Movie.nfo"
    media.write_bytes(b"media")
    nfo.write_bytes(b"raw nfo")
    uploader = _RecordingUploader()
    service = ContributionService(
        crowdnfo=uploader,
        mediainfo=_StaticMediaInfo(b"mediainfo"),
    )
    result = await service.contribute(
        ContributionItem(
            "Movie-GROUP",
            media,
            nfo_path=nfo,
            source_category="movies",
            media_sha256="AB" * 32,
            filelist=[{"filePath": "folder\\Movie.mkv", "fileSizeBytes": 5}],
        ),
        include_nfo=True,
        include_mediainfo=True,
        include_filelist=True,
    )

    assert result.status == "success"
    assert [name for name, _kwargs in uploader.calls] == [
        "nfo",
        "mediainfo",
        "filelist",
    ]
    assert uploader.calls[-1][1]["entries"] == [
        {"file_path": "folder/Movie.mkv", "file_size_bytes": 5}
    ]
    assert all(kwargs["category"] == "Movies" for _name, kwargs in uploader.calls)
    assert all(kwargs["media_sha256"] == "ab" * 32 for _name, kwargs in uploader.calls)


@pytest.mark.parametrize(
    "filelist",
    [
        [{"file_path": "/absolute.mkv", "file_size_bytes": 1}],
        [{"file_path": "../escape.mkv", "file_size_bytes": 1}],
        [{"file_path": "movie\n.mkv", "file_size_bytes": 1}],
        [{"file_path": "movie.mkv", "file_size_bytes": True}],
        [{"file_path": "movie.mkv", "file_size_bytes": -1}],
        [{"file_path": "", "file_size_bytes": 1}],
    ],
)
@pytest.mark.asyncio
async def test_contribution_rejects_unsafe_filelists_as_component_failure(
    tmp_path: Path,
    filelist: list[dict[str, object]],
) -> None:
    service = ContributionService(
        crowdnfo=_RecordingUploader(), mediainfo=_StaticMediaInfo(b"json")
    )
    result = await service.contribute(
        ContributionItem("Release", tmp_path / "movie.mkv", filelist=filelist),
        include_nfo=False,
        include_mediainfo=False,
        include_filelist=True,
    )

    assert result.status == "failed"
    assert result.components["filelist"].error == "invalid input"


@pytest.mark.asyncio
async def test_contribution_reports_invalid_nfo_and_empty_mediainfo_independently(
    tmp_path: Path,
) -> None:
    media = tmp_path / "Movie.mkv"
    wrong_nfo = tmp_path / "Movie.txt"
    media.write_bytes(b"media")
    wrong_nfo.write_bytes(b"not an nfo")
    service = ContributionService(
        crowdnfo=_RecordingUploader(), mediainfo=_StaticMediaInfo(b"")
    )
    result = await service.contribute(
        ContributionItem("Release", media, nfo_path=wrong_nfo),
        include_nfo=True,
        include_mediainfo=True,
        include_filelist=False,
    )

    assert result.status == "failed"
    assert result.components["nfo"].error == "invalid input"
    assert result.components["mediainfo"].error == "invalid input"


@pytest.mark.asyncio
async def test_contribution_rejects_nfo_outside_media_directory(tmp_path: Path) -> None:
    media = tmp_path / "media/Movie.mkv"
    nfo = tmp_path / "other/Movie.nfo"
    media.parent.mkdir()
    nfo.parent.mkdir()
    media.write_bytes(b"media")
    nfo.write_bytes(b"nfo")
    result = await ContributionService(
        crowdnfo=_RecordingUploader(), mediainfo=_StaticMediaInfo(b"json")
    ).contribute(
        ContributionItem("Release", media, nfo_path=nfo),
        include_nfo=True,
        include_mediainfo=False,
        include_filelist=False,
    )

    assert result.status == "failed"
    assert result.components["nfo"].error == "invalid input"


@pytest.mark.asyncio
async def test_crowdnfo_retries_gets_with_retry_after_and_exponential_backoff() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "1.5"}),
            httpx.Response(503),
            httpx.Response(200, json={"fileId": "nfo-id"}),
        ]
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: next(responses))
    ) as http:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.test",
            http_client=http,
            max_retries=2,
            request_interval=0,
            sleep=record_sleep,
        )
        metadata = await client.lookup(release_name="Release")

    assert metadata.file_id == "nfo-id"
    assert delays == [1.5, 0.5]


@pytest.mark.asyncio
async def test_crowdnfo_exhausted_get_retry_raises_http_error() -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.test",
            http_client=http,
            max_retries=1,
            sleep=lambda _delay: asyncio.sleep(0),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.lookup(release_name="Release")

    assert request_count == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrency": 0},
        {"max_retries": -1},
        {"base_url": "ftp://crowdnfo.test"},
        {"base_url": "https://crowdnfo.test/nested"},
    ],
)
def test_crowdnfo_rejects_invalid_client_configuration(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        CrowdNFOClient(**kwargs)


@pytest.mark.asyncio
async def test_crowdnfo_lookup_validates_response_and_media_hashes() -> None:
    matching_hash = "ab" * 32
    payloads = iter(
        [
            {"missing": "file id"},
            {
                "fileId": "wrong-hash",
                "release": {"variants": [{"mediaSha256": "cd" * 32}]},
            },
            {
                "fileId": "matching-hash",
                "fileSizeBytes": "12",
                "release": {"variants": [{"mediaSha256": matching_hash.upper()}]},
            },
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=next(payloads))
        )
    ) as http:
        client = CrowdNFOClient(base_url="https://crowdnfo.test", http_client=http)
        with pytest.raises(ValueError, match="fileId"):
            await client.lookup(release_name="Missing")
        with pytest.raises(LookupError, match="different media hash"):
            await client.lookup(release_name="Wrong.Hash", media_sha256=matching_hash)
        metadata = await client.lookup(
            release_name="Matching.Hash", media_sha256=matching_hash
        )

    assert metadata.file_size_bytes == 12
    assert metadata.media_hashes == frozenset({matching_hash})
    assert metadata.hash_verified is True


@pytest.mark.asyncio
async def test_crowdnfo_download_by_file_id_skips_metadata_lookup() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"raw\xffnfo")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.test",
            api_key=_Secret(),
            http_client=http,
        )
        content = await client.download_nfo(file_id="id/with spaces")

    assert content == b"raw\xffnfo"
    assert len(captured) == 1
    assert captured[0].url.path == "/api/files/id/with spaces/download"
    assert b"id%2Fwith%20spaces" in captured[0].url.raw_path
    assert captured[0].headers["X-Api-Key"] == "decrypted"


@pytest.mark.asyncio
async def test_crowdnfo_upload_response_and_retry_behavior() -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(201, content=b""),
            httpx.Response(201, json=[]),
            httpx.Response(201, content=b""),
        ]
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.test",
            http_client=http,
            max_retries=5,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.upload_nfo(
                release_name="Release",
                filename="Release.nfo",
                content=b"nfo",
            )
        assert (
            await client.upload_mediainfo(
                release_name="Release",
                filename="Release.json",
                content=b"json",
            )
            is None
        )
        assert (
            await client.upload_nfo(
                release_name="Release",
                filename="Release.nfo",
                content=b"nfo",
            )
            is None
        )
        assert (
            await client.upload_filelist(
                release_name="Release",
                entries=[{"file_path": "Release.mkv", "file_size_bytes": 5}],
            )
            is None
        )

    assert len(requests) == 4
    assert b'name="FileHash"' not in requests[1].content
    assert "fileHash" not in requests[3].content.decode()


@pytest.mark.asyncio
async def test_crowdnfo_requires_lookup_identifier_and_nonblank_file_id() -> None:
    client = CrowdNFOClient(base_url="https://crowdnfo.test")
    try:
        with pytest.raises(ValueError, match="release_name is required"):
            await client.lookup()
        with pytest.raises(ValueError, match="identifiers cannot be blank"):
            await client.download_nfo(file_id="")
    finally:
        await client.aclose()
