from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from backend import main as main_module
from backend.core.files import MismatchCleanupPolicy
from backend.core.settings import AppSettings, DownloadMode, SettingsPatch
from backend.db.settings import SettingsStore
from backend.main import create_app


def _read_file_artifacts(directory: Path) -> bytes:
    return b"".join(
        artifact.read_bytes() for artifact in directory.iterdir() if artifact.is_file()
    )


@pytest.mark.asyncio
async def test_settings_defaults_are_complete_generic_and_persisted(
    tmp_path: Path,
) -> None:
    database = tmp_path / "settings.sqlite"
    first = SettingsStore(database)
    await first.initialize()
    settings = await first.get()

    assert str(settings.crowdnfo.base_url).rstrip("/") == "https://crowdnfo.net"
    assert settings.download_mode is DownloadMode.OFF
    assert settings.auto_recheck is True
    assert settings.nfo_mismatch_policy is MismatchCleanupPolicy.KEEP
    assert settings.contribute.enabled is False
    assert settings.match_strategy == "hash_then_release_name"
    assert settings.hash_max_size_bytes > 0
    assert settings.path_mappings == []
    assert settings.category_mappings == {}
    assert settings.backfill_cron == "0 3 * * *"
    assert settings.dry_run is True
    assert settings.qbittorrent.enabled is False
    assert settings.sabnzbd.enabled is False
    assert settings.radarr.enabled is False
    assert settings.sonarr.enabled is False
    assert settings.umlautadaptarr.enabled is False
    serialized = json.dumps(settings.model_dump(mode="json"))
    assert "10.10.3." not in serialized
    assert "/home/ubuntu/media" not in serialized

    await first.update(
        SettingsPatch(
            dry_run=False,
            backfill_cron="15 4 * * 1",
            nfo_mismatch_policy=MismatchCleanupPolicy.REMOVE,
        )
    )
    await first.close()
    reopened = SettingsStore(database)
    await reopened.initialize()
    persisted = await reopened.get()
    await reopened.close()

    assert persisted.dry_run is False
    assert persisted.backfill_cron == "15 4 * * 1"
    assert persisted.nfo_mismatch_policy is MismatchCleanupPolicy.REMOVE


@pytest.mark.parametrize(
    "patch",
    [
        {"crowdnfo": {"base_url": "javascript:alert(1)"}},
        {"backfill_cron": "not a cron expression"},
        {"path_mappings": [{"remote_root": "relative/data", "local_root": "/data"}]},
        {"path_mappings": [{"remote_root": "/data", "local_root": "relative/data"}]},
        {"nfo_mismatch_policy": "delete_media"},
    ],
    ids=[
        "unsafe-url",
        "invalid-cron",
        "relative-remote",
        "relative-local",
        "unsafe-mismatch-policy",
    ],
)
def test_settings_patch_validates_urls_cron_and_absolute_mappings(
    patch: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        SettingsPatch.model_validate(patch)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("keep", MismatchCleanupPolicy.KEEP),
        ("remove", MismatchCleanupPolicy.REMOVE),
    ],
)
def test_settings_patch_accepts_only_supported_nfo_mismatch_policies(
    value: str,
    expected: MismatchCleanupPolicy,
) -> None:
    patch = SettingsPatch.model_validate({"nfo_mismatch_policy": value})

    assert patch.nfo_mismatch_policy is expected


def test_runtime_composition_forwards_recheck_and_mismatch_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class StubConnector:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    class RecordingRepairService:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(main_module, "CrowdNFOClient", StubConnector)
    monkeypatch.setattr(main_module, "QBitConnector", StubConnector)
    monkeypatch.setattr(
        main_module,
        "TorrentRepairService",
        RecordingRepairService,
    )
    media_root = tmp_path / "media"
    media_root.mkdir()
    settings = AppSettings.model_validate(
        {
            "crowdnfo": {"api_key": "configured"},
            "qbittorrent": {
                "enabled": True,
                "base_url": "http://qbittorrent:8080",
            },
            "path_mappings": [{"remote_root": "/data", "local_root": str(media_root)}],
            "auto_recheck": False,
            "nfo_mismatch_policy": "remove",
            "dry_run": False,
        }
    )
    services = main_module._DefaultServices(
        settings=SimpleNamespace(),
        operations=SimpleNamespace(),
    )

    services._compose_bundle(settings)

    assert captured["auto_recheck"] is False
    assert captured["keep_mismatch"] is False


def test_settings_patch_accepts_public_custom_connector_and_path_values() -> None:
    patch = SettingsPatch.model_validate(
        {
            "crowdnfo": {"base_url": "https://community.example/api"},
            "qbittorrent": {
                "enabled": True,
                "base_url": "http://qbittorrent:8080",
            },
            "backfill_cron": "0 */6 * * *",
            "path_mappings": [
                {"remote_root": "/downloads", "local_root": "/media/downloads"}
            ],
            "category_mappings": {"movies": "radarr", "shows": "sonarr"},
        }
    )

    assert patch.qbittorrent is not None
    assert patch.path_mappings[0].remote_root == "/downloads"
    assert patch.category_mappings == {"movies": "radarr", "shows": "sonarr"}


@pytest.mark.asyncio
async def test_secrets_are_write_only_encrypted_and_blank_updates_preserve_them(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "settings.sqlite"
    master_key = Fernet.generate_key().decode()
    store = SettingsStore(database, master_key=master_key)
    await store.initialize()
    caplog.set_level(logging.DEBUG)
    await store.update(
        SettingsPatch.model_validate(
            {
                "crowdnfo": {"api_key": "crowd-secret-value"},
                "qbittorrent": {"password": "qbit-secret-value"},
                "sabnzbd": {"api_key": "sab-secret-value"},
                "application_api_token": "app-secret-value",
            }
        )
    )
    public = await store.public_view()

    public_json = json.dumps(public, default=str)
    assert "crowd-secret-value" not in public_json
    assert "qbit-secret-value" not in public_json
    assert "sab-secret-value" not in public_json
    assert "app-secret-value" not in public_json
    assert "api_key" not in public["crowdnfo"]
    assert "password" not in public["qbittorrent"]
    assert "application_api_token" not in public
    assert public["secrets_configured"] == {
        "crowdnfo_api_key": True,
        "qbittorrent_password": True,
        "sabnzbd_api_key": True,
        "application_api_token": True,
    }

    await store.update(
        SettingsPatch.model_validate(
            {
                "crowdnfo": {"api_key": ""},
                "qbittorrent": {"password": ""},
                "sabnzbd": {"api_key": ""},
                "application_api_token": "",
            }
        )
    )
    retained = await store.get()
    assert retained.crowdnfo.api_key.get_secret_value() == "crowd-secret-value"
    assert retained.qbittorrent.password.get_secret_value() == "qbit-secret-value"
    assert retained.sabnzbd.api_key.get_secret_value() == "sab-secret-value"
    assert retained.application_api_token.get_secret_value() == "app-secret-value"
    await store.close()

    all_logs = caplog.text
    all_artifacts = await asyncio.to_thread(_read_file_artifacts, tmp_path)
    for secret in (
        "crowd-secret-value",
        "qbit-secret-value",
        "sab-secret-value",
        "app-secret-value",
    ):
        assert secret not in all_logs
        assert secret.encode() not in all_artifacts


class FakeSettings:
    def __init__(self) -> None:
        self.updated: dict[str, Any] | None = None

    async def public_view(self) -> dict[str, Any]:
        return {
            "crowdnfo": {"base_url": "https://crowdnfo.net"},
            "download_mode": "off",
            "dry_run": True,
            "secrets_configured": {"crowdnfo_api_key": True},
        }

    async def update_public(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.updated = patch
        return (await self.public_view()) | {"dry_run": patch.get("dry_run", True)}


class FakeDashboard:
    async def snapshot(self) -> dict[str, Any]:
        return {
            "connector_health": {},
            "counters": {"fetched": 0, "repaired": 0, "uploaded": 0},
            "recent_activity": [],
            "stuck_torrents": [],
        }


class FakeActions:
    async def scan_repair(self) -> str:
        return "scan-job-1"

    async def repair_torrent(self, torrent_hash: str) -> str:
        return f"repair-{torrent_hash}"

    async def retry_miss(self, miss_id: str) -> str:
        return f"retry-{miss_id}"


class FakeConnectors:
    async def test(self, connector: str) -> dict[str, Any]:
        raise ConnectionError(f"{connector} unavailable; password=must-not-leak")


def make_test_app(*, api_token: str | None = None) -> Any:
    services = SimpleNamespace(
        settings=FakeSettings(),
        dashboard=FakeDashboard(),
        actions=FakeActions(),
        connectors=FakeConnectors(),
    )
    return create_app(services=services, api_token=api_token)


@pytest.mark.asyncio
async def test_fastapi_health_dashboard_settings_and_action_smoke() -> None:
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        health = await client.get("/api/health")
        dashboard = await client.get("/api/dashboard")
        settings = await client.get("/api/settings")
        updated = await client.put("/api/settings", json={"dry_run": False})
        action = await client.post("/api/actions/scan-repair")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert dashboard.status_code == 200
    assert dashboard.json()["stuck_torrents"] == []
    assert settings.status_code == 200
    assert "api_key" not in settings.json()["crowdnfo"]
    assert updated.status_code == 200 and updated.json()["dry_run"] is False
    assert action.status_code == 202
    assert action.json() == {"job_id": "scan-job-1", "status": "accepted"}
    for response in (health, dashboard, settings, updated, action):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_connector_test_degrades_to_sanitized_503_payload() -> None:
    app = make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/connectors/qbittorrent/test")

    assert response.status_code == 503
    assert response.json() == {
        "connector": "qbittorrent",
        "status": "unavailable",
        "detail": "connection failed",
    }
    assert "must-not-leak" not in response.text


@pytest.mark.asyncio
async def test_optional_application_token_protects_api_but_not_health() -> None:
    app = make_test_app(api_token="local-api-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        health = await client.get("/api/health")
        unauthenticated = await client.get("/api/settings")
        authenticated = await client.get(
            "/api/settings",
            headers={"Authorization": "Bearer local-api-token"},
        )

    assert health.status_code == 200
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "authentication required"}
    assert authenticated.status_code == 200
