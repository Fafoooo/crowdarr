from __future__ import annotations

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
async def test_validate_api_key_uses_authenticated_current_user_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"username": "crowdarrr-user"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = CrowdNFOClient(
            base_url="https://crowdnfo.example/api/",
            api_key="profile-api-key",
            http_client=http_client,
        )
        await client.validate_api_key()

    assert len(requests) == 1
    assert requests[0].url.path == "/api/user/me"
    assert requests[0].headers["X-Api-Key"] == "profile-api-key"


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
