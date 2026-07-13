from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from backend import main as main_module
from backend.connectors.health import ConnectorHealth
from backend.connectors.sab import SABCompletionEvent
from backend.core.library import LibraryMediaItem
from backend.core.settings import AppSettings, ConnectorSettings, SettingsPatch
from backend.db.settings import SettingsEncryptionError, SettingsStore
from backend.main import create_app
from backend.runtime import ActionQueueFull, WorkflowOutcome

SAB_SECRET_HEADER = "X-Crowdarr-SAB-Secret"


class MutableSettings:
    def __init__(self, token: str = "") -> None:
        self.token = token
        self.updated: dict[str, Any] | None = None
        self.fail_token_load = False

    async def get(self) -> Any:
        if self.fail_token_load:
            raise ConnectionError("database password=must-not-leak")
        return SimpleNamespace(application_api_token=SecretStr(self.token))

    async def public_view(self) -> dict[str, Any]:
        return {"download_mode": "off", "dry_run": True}

    async def update_public(self, patch: dict[str, Any]) -> dict[str, Any]:
        if patch.get("backfill_cron") == "invalid":
            raise ValueError("invalid cron")
        self.updated = patch
        return {"download_mode": "off", "dry_run": patch.get("dry_run", True)}


class DashboardStub:
    async def snapshot(self) -> dict[str, Any]:
        return {
            "connectors": [],
            "counters": {},
            "recent_activity": [],
            "stuck_torrents": [],
        }


class ActionsStub:
    def __init__(self, *, full: bool = False) -> None:
        self.full = full

    async def scan_repair(self) -> str:
        if self.full:
            raise ActionQueueFull("busy")
        return "scan-job"

    async def repair_torrent(self, torrent_hash: str) -> str:
        if self.full:
            raise ActionQueueFull("busy")
        return f"repair-{torrent_hash}"

    async def retry_miss(self, miss_id: str) -> str:
        if self.full:
            raise ActionQueueFull("busy")
        return f"retry-{miss_id}"

    async def job_status(self, job_id: str) -> dict[str, Any]:
        if job_id == "missing":
            raise KeyError(job_id)
        return {
            "job_id": job_id,
            "kind": "repair_torrent",
            "status": "success",
            "result": {},
        }


class ConnectorsStub:
    def __init__(self) -> None:
        self.last_patch: dict[str, Any] | None = None

    async def test(
        self,
        connector: str,
        patch: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        self.last_patch = patch
        return {"connector": connector, "status": "healthy"}


def service_stub(
    *,
    settings: MutableSettings | None = None,
    actions: ActionsStub | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        settings=settings or MutableSettings(),
        dashboard=DashboardStub(),
        actions=actions or ActionsStub(),
        connectors=ConnectorsStub(),
    )


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def assert_security_headers(response: httpx.Response) -> None:
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == (
        "camera=(), geolocation=(), microphone=()"
    )
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


@pytest.mark.asyncio
async def test_persisted_api_token_updates_immediately_and_health_stays_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CROWDARRR_API_TOKEN", raising=False)
    settings = MutableSettings("first-token")
    app = create_app(services=service_stub(settings=settings), api_token=None)

    async with client_for(app) as client:
        health = await client.get("/api/health")
        missing = await client.get("/api/settings")
        wrong_scheme = await client.get(
            "/api/settings", headers={"Authorization": "Basic first-token"}
        )
        first = await client.get(
            "/api/settings", headers={"Authorization": "bEaReR first-token"}
        )
        settings.token = "second-token"
        stale = await client.get(
            "/api/settings", headers={"Authorization": "Bearer first-token"}
        )
        second = await client.get(
            "/api/settings", headers={"Authorization": "Bearer second-token"}
        )

    assert health.status_code == 200
    assert missing.status_code == 401
    assert wrong_scheme.status_code == 401
    assert first.status_code == 200
    assert stale.status_code == 401
    assert second.status_code == 200
    for response in (health, missing, wrong_scheme, first, stale, second):
        assert_security_headers(response)


@pytest.mark.asyncio
async def test_api_fails_closed_when_persisted_token_cannot_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CROWDARRR_API_TOKEN", raising=False)
    settings = MutableSettings()
    settings.fail_token_load = True
    app = create_app(services=service_stub(settings=settings), api_token=None)

    async with client_for(app) as client:
        response = await client.get("/api/settings")

    assert response.status_code == 503
    assert response.json() == {"detail": "security configuration is unavailable"}
    assert "must-not-leak" not in response.text
    assert_security_headers(response)


@pytest.mark.asyncio
async def test_all_action_endpoints_return_bounded_queue_backpressure() -> None:
    app = create_app(
        services=service_stub(actions=ActionsStub(full=True)), api_token=""
    )

    async with client_for(app) as client:
        responses = (
            await client.post("/api/actions/scan-repair"),
            await client.post("/api/torrents/abc123/repair"),
            await client.post("/api/actions/misses/miss-7/retry"),
        )

    for response in responses:
        assert response.status_code == 503
        assert response.json() == {"detail": "action queue is full"}
        assert response.headers["Retry-After"] == "1"
        assert_security_headers(response)


@pytest.mark.asyncio
async def test_torrent_repair_and_miss_retry_return_trackable_jobs() -> None:
    app = create_app(services=service_stub(), api_token="")

    async with client_for(app) as client:
        repair = await client.post("/api/torrents/abc123/repair")
        retry = await client.post("/api/actions/misses/miss-7/retry")
        job = await client.get("/api/jobs/repair-abc123")
        missing = await client.get("/api/jobs/missing")

    assert repair.status_code == 202
    assert repair.json() == {"job_id": "repair-abc123", "status": "accepted"}
    assert retry.status_code == 202
    assert retry.json() == {"job_id": "retry-miss-7", "status": "accepted"}
    assert job.status_code == 200
    assert job.json()["status"] == "success"
    assert missing.status_code == 404
    assert missing.json() == {"detail": "job not found"}


@pytest.mark.asyncio
async def test_settings_validation_and_runtime_reload_are_atomic() -> None:
    settings = MutableSettings()
    reloads = 0

    async def reload_runtime() -> None:
        nonlocal reloads
        reloads += 1

    services = service_stub(settings=settings)
    services.reload_runtime = reload_runtime
    app = create_app(services=services, api_token="")

    async with client_for(app) as client:
        invalid = await client.put("/api/settings", json={"backfill_cron": "invalid"})
        valid = await client.put("/api/settings", json={"dry_run": False})

    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "invalid settings"}
    assert valid.status_code == 200
    assert valid.json()["dry_run"] is False
    assert settings.updated == {"dry_run": False}
    assert reloads == 1


@pytest.mark.asyncio
async def test_settings_encryption_failure_returns_actionable_safe_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenSettings(MutableSettings):
        async def update_public(self, patch: dict[str, Any]) -> dict[str, Any]:
            raise SettingsEncryptionError(
                "CROWDARRR_MASTER_KEY is malformed; secret=must-not-leak"
            )

    app = create_app(services=service_stub(settings=BrokenSettings()), api_token="")
    caplog.set_level("ERROR")

    async with client_for(app) as client:
        response = await client.put("/api/settings", json={"dry_run": False})

    assert response.status_code == 503
    assert response.json() == {
        "detail": (
            "settings encryption is unavailable; check CROWDARR_MASTER_KEY "
            "(legacy CROWDARRR_MASTER_KEY) and server logs"
        )
    }
    assert "must-not-leak" not in response.text
    assert "must-not-leak" not in caplog.text


@pytest.mark.asyncio
async def test_connector_routes_reject_unknown_names_and_serialize_results() -> None:
    services = service_stub()
    app = create_app(services=services, api_token="")

    async with client_for(app) as client:
        unknown = await client.post("/api/connectors/not-a-service/test")
        known = await client.post(
            "/api/connectors/crowdnfo/test",
            json={
                "api_key": "draft-key",
                "base_url": "https://community.example",
            },
        )

    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "connector not found"}
    assert known.status_code == 200
    assert known.json() == {"connector": "crowdnfo", "status": "healthy"}
    assert services.connectors.last_patch == {
        "api_key": "draft-key",
        "base_url": "https://community.example",
    }


@pytest.mark.asyncio
async def test_connector_test_rejects_an_invalid_draft_as_user_input() -> None:
    class Settings:
        async def get(self) -> AppSettings:
            return AppSettings()

    default_services = main_module._DefaultServices(
        settings=cast(Any, Settings()),
        operations=cast(Any, SimpleNamespace()),
    )
    services = service_stub()
    services.connectors = default_services.connectors
    app = create_app(services=services, api_token="")

    async with client_for(app) as client:
        response = await client.post(
            "/api/connectors/crowdnfo/test",
            json={"api_key": "draft-key", "base_url": "not-a-url"},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid connector settings"}


@pytest.mark.asyncio
async def test_logs_use_safe_bounds_and_degrade_when_service_is_absent() -> None:
    observed: list[int] = []

    class SyncLogs:
        def list(self, *, limit: int) -> dict[str, Any]:
            observed.append(limit)
            return {"items": [{"limit": limit}], "next_cursor": None}

    with_logs = service_stub()
    with_logs.logs = SyncLogs()
    without_logs = service_stub()
    app_with_logs = create_app(services=with_logs, api_token="")
    app_without_logs = create_app(services=without_logs, api_token="")

    async with client_for(app_with_logs) as client:
        low = await client.get("/api/logs?limit=0")
        high = await client.get("/api/logs?limit=9999")
    async with client_for(app_without_logs) as client:
        missing = await client.get("/api/logs")

    assert observed == [1, 1_000]
    assert low.json()["items"] == [{"limit": 1}]
    assert high.json()["items"] == [{"limit": 1_000}]
    assert missing.json() == {"items": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_sab_webhook_is_disabled_until_a_dedicated_secret_exists() -> None:
    app = create_app(services=SimpleNamespace(), api_token="", sab_webhook_secret="")

    async with client_for(app) as client:
        response = await client.post("/api/webhooks/sabnzbd", json={})

    assert response.status_code == 503
    assert response.json() == {"detail": "SAB webhook secret is not configured"}
    assert_security_headers(response)


@pytest.mark.asyncio
async def test_sab_webhook_rejects_invalid_length_and_payload_before_dispatch() -> None:
    events: list[SABCompletionEvent] = []

    async def handle(event: SABCompletionEvent) -> dict[str, bool]:
        events.append(event)
        return {"accepted": True}

    services = SimpleNamespace(handle_sab_completion=handle)
    app = create_app(services=services, api_token="", sab_webhook_secret="hook-secret")

    async with client_for(app) as client:
        invalid_length = await client.post(
            "/api/webhooks/sabnzbd",
            content=b"{}",
            headers={
                SAB_SECRET_HEADER: "hook-secret",
                "Content-Type": "application/json",
                "Content-Length": "invalid",
            },
        )
        invalid_payload = await client.post(
            "/api/webhooks/sabnzbd",
            json={"release_name": "", "storage_path": "/data/download"},
            headers={SAB_SECRET_HEADER: "hook-secret"},
        )

    assert invalid_length.status_code == 413
    assert invalid_payload.status_code == 422
    assert invalid_payload.json() == {"detail": "invalid SAB completion payload"}
    assert events == []


@pytest.mark.asyncio
async def test_sab_webhook_reports_unavailable_and_failed_workflows() -> None:
    unavailable = create_app(
        services=SimpleNamespace(), api_token="", sab_webhook_secret="hook-secret"
    )

    class FailedWorkflow:
        def handle_sab_completion(self, event: SABCompletionEvent) -> WorkflowOutcome:
            assert event.nzo_id == "nzo-1"
            return WorkflowOutcome(
                job_id="sab:nzo-1",
                status="failed",
                result={"reason": "history mismatch"},
            )

    failed = create_app(
        services=FailedWorkflow(), api_token="", sab_webhook_secret="hook-secret"
    )
    payload = {
        "name": "Movie.2026-GROUP",
        "storage": "/data/downloads/Movie",
        "category": "movies",
        "nzo_id": "nzo-1",
    }
    headers = {SAB_SECRET_HEADER: "hook-secret"}

    async with client_for(unavailable) as client:
        missing = await client.post(
            "/api/webhooks/sabnzbd", json=payload, headers=headers
        )
    async with client_for(failed) as client:
        rejected = await client.post(
            "/api/webhooks/sabnzbd", json=payload, headers=headers
        )

    assert missing.status_code == 503
    assert missing.json() == {"detail": "SAB completion handling is unavailable"}
    assert rejected.status_code == 422
    assert rejected.json() == {
        "job_id": "sab:nzo-1",
        "status": "failed",
        "result": {"reason": "history mismatch"},
    }


@pytest.mark.asyncio
async def test_static_spa_serves_assets_routes_and_blocks_api_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>Crowdarrr SPA</main>", encoding="utf-8")
    (frontend / "app.js").write_text("window.crowdarrr = true", encoding="utf-8")
    outside = tmp_path / "private.txt"
    outside.write_text("must-not-be-served", encoding="utf-8")
    monkeypatch.setenv("CROWDARRR_FRONTEND_DIR", str(frontend))
    app = create_app(services=service_stub(), api_token="")

    async with client_for(app) as client:
        index = await client.get("/")
        asset = await client.get("/app.js")
        client_route = await client.get("/settings/connectors")
        missing_api = await client.get("/api/not-a-route")
        traversal = await client.get("/%2e%2e/private.txt")

    assert index.status_code == 200
    assert index.text == "<main>Crowdarrr SPA</main>"
    assert asset.status_code == 200
    assert asset.text == "window.crowdarrr = true"
    assert client_route.status_code == 200
    assert client_route.text == index.text
    assert missing_api.status_code == 404
    assert traversal.status_code == 404
    assert "must-not-be-served" not in traversal.text


@pytest.mark.asyncio
async def test_owned_service_lifespan_initializes_and_closes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class OwnedServices:
        async def initialize(self) -> None:
            calls.append("initialize")

        async def close(self) -> None:
            calls.append("close")

    owned = OwnedServices()
    monkeypatch.setattr(main_module, "_default_services", lambda: owned)
    app = create_app(api_token="")

    async with app.router.lifespan_context(app):
        assert calls == ["initialize"]

    assert calls == ["initialize", "close"]


@pytest.mark.asyncio
async def test_default_application_lifecycle_creates_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CROWDARR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("TZ", "UTC")
    app = create_app(api_token="")

    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await client.get("/api/health")
        assert response.status_code == 200
        assert (tmp_path / "crowdarr.sqlite3").is_file()

    assert_security_headers(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("validation_result", "status_code", "expected"),
    [
        (True, None, ConnectorHealth(True)),
        (
            False,
            None,
            ConnectorHealth(
                True,
                detail="API reachable; profile key could not be verified",
                degraded=True,
            ),
        ),
        (None, 401, ConnectorHealth(False, detail="authentication failed")),
        (None, 503, ConnectorHealth(False, detail="service unavailable")),
    ],
)
async def test_crowdnfo_health_validates_saved_credentials(
    validation_result: bool | None,
    status_code: int | None,
    expected: ConnectorHealth,
) -> None:
    class Client:
        async def validate_api_key(self) -> bool:
            if status_code is None:
                assert validation_result is not None
                return validation_result
            request = httpx.Request("GET", "https://crowdnfo.net/api/test")
            response = httpx.Response(status_code, request=request)
            raise httpx.HTTPStatusError("failed", request=request, response=response)

    health = await main_module._CrowdNFOHealth(cast(Any, Client())).healthcheck()

    assert health == expected


@pytest.mark.asyncio
async def test_legacy_database_and_master_key_remain_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master_key = Fernet.generate_key().decode("ascii")
    legacy_database = tmp_path / "crowdarrr.sqlite3"
    legacy_store = SettingsStore(legacy_database, master_key=master_key)
    await legacy_store.initialize()
    await legacy_store.update(SettingsPatch(crowdnfo={"api_key": "legacy-profile-key"}))
    await legacy_store.close()
    monkeypatch.setenv("CROWDARR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CROWDARRR_MASTER_KEY", master_key)

    app = create_app(api_token="")

    async with app.router.lifespan_context(app), client_for(app) as client:
        response = await client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["secrets_configured"]["crowdnfo_api_key"] is True
    assert legacy_database.is_file()
    assert not (tmp_path / "crowdarr.sqlite3").exists()


def test_corrected_environment_name_takes_precedence_even_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CROWDARRR_API_TOKEN", "legacy-token")
    monkeypatch.setenv("CROWDARR_API_TOKEN", "")

    assert main_module._env("API_TOKEN") == ""

    monkeypatch.delenv("CROWDARR_API_TOKEN")
    assert main_module._env("API_TOKEN") == "legacy-token"


@pytest.mark.asyncio
async def test_library_title_recovery_is_best_effort(tmp_path: Path) -> None:
    items = [
        LibraryMediaItem("Renamed.Release", tmp_path / "one.mkv"),
        LibraryMediaItem("Keep.Release", tmp_path / "two.mkv"),
    ]

    class Library:
        async def scan(self) -> list[LibraryMediaItem]:
            return items

    class Umlaut:
        async def recover_release_name(self, title: str) -> str | None:
            if title == "Keep.Release":
                raise ConnectionError("offline")
            return "Original.Release-GROUP"

    connector = main_module._ReleaseResolvingLibraryConnector(
        cast(Any, Library()), cast(Any, Umlaut())
    )

    resolved = await connector.scan()

    assert resolved[0].release_name == "Original.Release-GROUP"
    assert resolved[1] is items[1]


@pytest.mark.asyncio
async def test_runtime_bundle_closes_each_shared_connector_once() -> None:
    calls: list[str] = []

    class Poller:
        def start(self) -> None:
            calls.append("poller-start")

        async def close(self) -> None:
            calls.append("poller-close")

    class Queue:
        async def close(self) -> None:
            calls.append("queue-close")

    class SharedConnector:
        async def aclose(self) -> None:
            calls.append("connector-close")

    shared = SharedConnector()
    bundle = main_module._RuntimeBundle(
        runtime=cast(Any, SimpleNamespace()),
        queue=cast(Any, Queue()),
        health_connectors={},
        closeables=(shared, shared),
        qbit_poller=cast(Any, Poller()),
    )

    bundle.start()
    await bundle.close()

    assert calls == [
        "poller-start",
        "poller-close",
        "queue-close",
        "connector-close",
    ]


@pytest.mark.asyncio
async def test_default_dashboard_and_logs_have_stable_empty_shapes() -> None:
    class Operations:
        async def get_counters(self) -> dict[str, int]:
            return {"fetched": 3, "unexpected": 99}

        async def list_activity(self, *, limit: int) -> list[Any]:
            assert limit == 5
            return []

    services = main_module._DefaultServices(
        settings=cast(Any, SimpleNamespace()),
        operations=cast(Any, Operations()),
    )

    dashboard = await services.dashboard.snapshot()
    logs = await services.logs.list(limit=5)

    assert dashboard == {
        "connectors": [],
        "counters": {
            "fetched": 3,
            "placed": 0,
            "repaired": 0,
            "uploaded": 0,
            "matches": 0,
            "misses": 0,
        },
        "recent_activity": [],
        "stuck_torrents": [],
        "dry_run": True,
    }
    assert logs == {"items": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_default_connector_test_reports_disabled_incomplete_and_health() -> None:
    settings = AppSettings()

    class Settings:
        async def get(self) -> AppSettings:
            return settings

    services = main_module._DefaultServices(
        settings=cast(Any, Settings()),
        operations=cast(Any, SimpleNamespace()),
    )

    disabled = await services.connectors.test("qbittorrent")
    crowdnfo_disabled = await services.connectors.test("crowdnfo")
    settings = AppSettings(
        qbittorrent=ConnectorSettings(enabled=True, base_url="http://qbittorrent:8080")
    )
    incomplete = await services.connectors.test("qbittorrent")

    class Healthy:
        async def healthcheck(self) -> ConnectorHealth:
            return ConnectorHealth(True, version="5.1.0")

    services.bundle = cast(
        Any,
        SimpleNamespace(health_connectors={"qbittorrent": Healthy()}),
    )
    healthy = await services.connectors.test("qbittorrent")

    assert disabled["status"] == "disabled"
    assert crowdnfo_disabled["status"] == "disabled"
    assert incomplete["status"] == "unhealthy"
    assert incomplete["message"] == "connector configuration is incomplete"
    assert healthy["status"] == "healthy"
    assert healthy["message"] == "5.1.0"
    assert isinstance(healthy["latency_ms"], int)


@pytest.mark.asyncio
async def test_default_connector_test_uses_draft_without_persisting_it() -> None:
    persisted = AppSettings()

    class Settings:
        async def get(self) -> AppSettings:
            return persisted

    class Healthy:
        async def healthcheck(self) -> ConnectorHealth:
            return ConnectorHealth(True, version="draft")

    class CandidateBundle:
        def __init__(self) -> None:
            self.health_connectors = {"crowdnfo": Healthy()}
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    services = main_module._DefaultServices(
        settings=cast(Any, Settings()),
        operations=cast(Any, SimpleNamespace()),
    )
    candidate_bundle = CandidateBundle()
    candidate: AppSettings | None = None

    def compose(settings: AppSettings) -> Any:
        nonlocal candidate
        candidate = settings
        return candidate_bundle

    services._compose_bundle = compose  # type: ignore[method-assign]

    result = await services.connectors.test(
        "crowdnfo",
        {
            "api_key": "draft-key",
            "base_url": "https://community.example",
        },
    )

    assert result["status"] == "healthy"
    assert result["message"] == "draft"
    assert candidate is not None
    assert str(candidate.crowdnfo.base_url).rstrip("/") == "https://community.example"
    assert candidate.crowdnfo.api_key.get_secret_value() == "draft-key"
    assert persisted.crowdnfo.api_key.get_secret_value() == ""
    assert candidate_bundle.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("connector_name", ["radarr", "sonarr"])
async def test_library_connector_draft_health_does_not_require_path_mapping(
    connector_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Settings:
        async def get(self) -> AppSettings:
            return AppSettings()

    class HealthyLibraryConnector:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["path_mapper"] is None

        async def healthcheck(self) -> ConnectorHealth:
            return ConnectorHealth(True, version="draft")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        main_module,
        "RadarrConnector" if connector_name == "radarr" else "SonarrConnector",
        HealthyLibraryConnector,
    )
    services = main_module._DefaultServices(
        settings=cast(Any, Settings()),
        operations=cast(Any, SimpleNamespace()),
    )

    result = await services.connectors.test(
        connector_name,
        {
            "enabled": True,
            "base_url": f"http://{connector_name}:8080",
            "api_key": "draft-key",
        },
    )

    assert result["status"] == "healthy"
    assert result["message"] == "draft"
