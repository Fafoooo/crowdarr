from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from backend.crowdnfo.client import CrowdNFOClient, UnsupportedLookupError
from backend.crowdnfo.endpoints import DEFAULT_CONTRACT


@pytest.mark.asyncio
async def test_download_nfo_uses_best_metadata_then_returns_file_bytes_unchanged() -> (
    None
):
    release_name = "Movie 2026 # Group"
    file_id = "a62c08a0-17b1-4adc-b93f-498be50e78c6"
    raw_nfo = b"\x00\xff\xfeCP437:\x80\x81\r\nLine two\r\n"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "fileId": file_id,
                    "originalFileName": "movie.nfo",
                    "fileSizeBytes": len(raw_nfo),
                    "fileType": "NFO",
                },
            )
        return httpx.Response(
            200,
            content=raw_nfo,
            headers={"Content-Type": "text/plain; charset=ibm437"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example",
            api_key="profile-api-key",
            http_client=http_client,
        )
        downloaded = await client.download_nfo(release_name=release_name)

    assert downloaded == raw_nfo
    assert hashlib.sha256(downloaded).digest() == hashlib.sha256(raw_nfo).digest()
    assert len(requests) == 2

    metadata_request, download_request = requests
    assert metadata_request.method == "GET"
    assert metadata_request.url.path == (f"/api/releases/{release_name}/files/best")
    assert b"Movie%202026%20%23%20Group" in metadata_request.url.raw_path
    assert metadata_request.url.params == httpx.QueryParams(
        {"type": "NFO", "raw": "false", "fallback": "false"}
    )
    assert download_request.url.path == f"/api/files/{file_id}/download"
    assert all(
        request.headers["X-Api-Key"] == "profile-api-key" for request in requests
    )


def test_default_contract_contains_only_verified_current_beta_routes() -> None:
    assert DEFAULT_CONTRACT.api_key_header == "X-Api-Key"
    assert DEFAULT_CONTRACT.current_user_path == "/api/user/me"
    assert DEFAULT_CONTRACT.best_file_path == "/api/releases/{release_name}/files/best"
    assert DEFAULT_CONTRACT.file_download_path == "/api/files/{file_id}/download"
    assert DEFAULT_CONTRACT.file_upload_path == "/api/releases/{release_name}/files"
    assert (
        DEFAULT_CONTRACT.filelist_upload_path
        == "/api/releases/{release_name}/filelists"
    )


@pytest.mark.asyncio
async def test_upload_nfo_uses_pascal_case_multipart_fields_and_api_key() -> None:
    raw_nfo = b"Release\r\n\xff\x80\r\n"
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"accepted": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example",
            api_key="upload-key",
            http_client=http_client,
        )
        await client.upload_nfo(
            release_name="Movie.2026-GROUP",
            filename="Movie.2026-GROUP.nfo",
            content=raw_nfo,
            media_sha256="ab" * 32,
            category="Movies",
        )

    request = captured[0]
    body = request.content
    assert request.method == "POST"
    assert request.url.path == "/api/releases/Movie.2026-GROUP/files"
    assert request.headers["X-Api-Key"] == "upload-key"
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="File"; filename="Movie.2026-GROUP.nfo"' in body
    assert b'name="FileType"' in body and b"NFO" in body
    assert b'name="OriginalFileName"' in body
    assert b'name="FileHash"' in body and (b"ab" * 32) in body
    assert b'name="Category"' in body and b"Movies" in body
    assert raw_nfo in body


@pytest.mark.asyncio
async def test_upload_filelist_uses_lower_camel_case_json() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"accepted": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example/api",
            api_key="filelist-key",
            http_client=http_client,
        )
        await client.upload_filelist(
            release_name="Show.S01E01-GROUP",
            category="TV",
            media_sha256="cd" * 32,
            entries=[
                {"file_path": "Show.S01E01.mkv", "file_size_bytes": 123456},
                {"file_path": "Show.S01E01.nfo", "file_size_bytes": 2048},
            ],
        )

    request = captured[0]
    assert request.url.path == "/api/releases/Show.S01E01-GROUP/filelists"
    assert request.headers["X-Api-Key"] == "filelist-key"
    assert json.loads(request.content) == {
        "releaseName": "Show.S01E01-GROUP",
        "category": "TV",
        "fileHash": "cd" * 32,
        "entries": [
            {"filePath": "Show.S01E01.mkv", "fileSizeBytes": 123456},
            {"filePath": "Show.S01E01.nfo", "fileSizeBytes": 2048},
        ],
    }


@pytest.mark.asyncio
async def test_hash_only_lookup_reports_capability_gap_without_network_request() -> (
    None
):
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(500, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example",
            api_key="profile-api-key",
            http_client=http_client,
        )
        with pytest.raises(UnsupportedLookupError, match="hash-only"):
            await client.lookup(media_sha256="ef" * 32)

    assert request_count == 0


@pytest.mark.asyncio
async def test_validate_api_key_probes_profile_key_compatible_lookup_route() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, json={"title": "Not Found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example/api/",
            api_key="profile-api-key",
            http_client=http_client,
        )
        verified = await client.validate_api_key()

    assert len(requests) == 1
    assert requests[0].url.path == (
        "/api/releases/__crowdarr_connection_test__/files/best"
    )
    assert requests[0].url.params == httpx.QueryParams(
        {"type": "NFO", "raw": "false", "fallback": "false"}
    )
    assert requests[0].headers["X-Api-Key"] == "profile-api-key"
    assert verified is False


@pytest.mark.asyncio
async def test_validate_api_key_rejects_lookup_route_authentication_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example",
            api_key="invalid-profile-api-key",
            http_client=http_client,
        )
        with pytest.raises(httpx.HTTPStatusError) as captured:
            await client.validate_api_key()

    assert captured.value.response.status_code == 401


@pytest.mark.asyncio
async def test_get_retries_transient_connection_errors_with_backoff() -> None:
    request_count = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count < 3:
            raise httpx.ConnectError("temporary outage", request=request)
        return httpx.Response(200, json={"fileId": "file-1"})

    async def sleep(delay: float) -> None:
        delays.append(delay)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example",
            api_key="profile-api-key",
            http_client=http_client,
            max_retries=2,
            request_interval=0,
            sleep=sleep,
        )
        result = await client.lookup(release_name="Release-GROUP")

    assert result.file_id == "file-1"
    assert request_count == 3
    assert delays == [0.25, 0.5]


@pytest.mark.asyncio
async def test_request_pacing_uses_the_injected_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    clock = iter((0.0, 0.0, 0.1, 0.5))

    async def injected_sleep(delay: float) -> None:
        delays.append(delay)

    async def unexpected_sleep(_delay: float) -> None:
        raise AssertionError("request pacing bypassed the injected sleep")

    monkeypatch.setattr("backend.crowdnfo.client.asyncio.sleep", unexpected_sleep)
    async with httpx.AsyncClient() as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example",
            http_client=http_client,
            request_interval=1.0,
            sleep=injected_sleep,
            monotonic=lambda: next(clock),
        )
        await client._pace_request()  # noqa: SLF001
        await client._pace_request()  # noqa: SLF001

    assert delays == [pytest.approx(0.9)]


@pytest.mark.asyncio
async def test_crowdnfo_bounds_parallel_get_requests() -> None:
    active = 0
    maximum_active = 0
    two_started = asyncio.Event()
    release_requests = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 2:
            two_started.set()
        await release_requests.wait()
        active -= 1
        return httpx.Response(200, json={"fileId": "nfo-id"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example",
            http_client=http_client,
            max_concurrency=2,
            request_interval=0,
        )
        requests = [
            asyncio.create_task(client.lookup(release_name=f"Release-{index}"))
            for index in range(5)
        ]
        await asyncio.wait_for(two_started.wait(), timeout=1)
        assert maximum_active == 2
        release_requests.set()
        await asyncio.gather(*requests)

    assert maximum_active == 2


@pytest.mark.asyncio
async def test_validate_api_key_rejects_missing_key_without_a_request() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example",
            http_client=http_client,
        )
        with pytest.raises(PermissionError, match="not configured"):
            await client.validate_api_key()

    assert request_count == 0
