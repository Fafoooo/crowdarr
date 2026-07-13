"""Encrypted single-document settings persistence."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from backend.core.settings import AppSettings, SettingsPatch

_CONNECTORS = (
    "qbittorrent",
    "sabnzbd",
    "radarr",
    "sonarr",
    "umlautadaptarr",
)


class SettingsStore:
    """Persist validated settings as encrypted JSON in SQLite.

    A caller-provided Fernet master key is preferred. When absent, a mode-0600
    key is generated beside the database so UI-provided secrets are still never
    written to SQLite as plaintext.
    """

    def __init__(self, database: Path, *, master_key: str | None = None) -> None:
        self._database = Path(database)
        self._master_key = master_key
        self._cipher: Fernet | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    def _get_cipher(self) -> Fernet:
        if self._cipher is None:
            key = (
                self._master_key.encode()
                if self._master_key is not None
                else self._local_key()
            )
            self._cipher = Fernet(key)
        return self._cipher

    def _local_key(self) -> bytes:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        key_path = self._database.with_suffix(f"{self._database.suffix}.key")
        try:
            return key_path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            try:
                descriptor = os.open(
                    key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                return key_path.read_bytes().strip()
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            return key

    def _seal(self, value: str) -> str:
        if not value:
            return ""
        return "enc:" + self._get_cipher().encrypt(value.encode()).decode()

    def _unseal(self, value: str) -> str:
        if not value:
            return ""
        if not value.startswith("enc:"):
            raise ValueError("settings database contains an unencrypted secret")
        try:
            return self._get_cipher().decrypt(value[4:].encode()).decode()
        except InvalidToken as exc:
            raise ValueError(
                "settings master key cannot decrypt stored secrets"
            ) from exc

    def _serialize(self, settings: AppSettings) -> str:
        data = settings.model_dump(mode="json")
        data["crowdnfo"]["api_key"] = self._seal(
            settings.crowdnfo.api_key.get_secret_value()
        )
        for connector_name in _CONNECTORS:
            connector = getattr(settings, connector_name)
            data[connector_name]["password"] = self._seal(
                connector.password.get_secret_value()
            )
            data[connector_name]["api_key"] = self._seal(
                connector.api_key.get_secret_value()
            )
        data["application_api_token"] = self._seal(
            settings.application_api_token.get_secret_value()
        )
        return json.dumps(data, separators=(",", ":"), sort_keys=True)

    def _deserialize(self, payload: str) -> AppSettings:
        data = json.loads(payload)
        data["crowdnfo"]["api_key"] = self._unseal(data["crowdnfo"]["api_key"])
        for connector_name in _CONNECTORS:
            connector = data[connector_name]
            connector["password"] = self._unseal(connector["password"])
            connector["api_key"] = self._unseal(connector["api_key"])
        data["application_api_token"] = self._unseal(data["application_api_token"])
        return AppSettings.model_validate(data)

    async def _connect(self) -> aiosqlite.Connection:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._database)
        await connection.execute("PRAGMA journal_mode=DELETE")
        await connection.execute("PRAGMA synchronous=FULL")
        return connection

    async def _initialize_unlocked(self) -> None:
        if self._initialized:
            return
        connection = await self._connect()
        try:
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            await connection.execute(
                """
                INSERT OR IGNORE INTO app_settings(singleton, payload, updated_at)
                VALUES (1, ?, ?)
                """,
                (self._serialize(AppSettings()), datetime.now(UTC).isoformat()),
            )
            await connection.commit()
        finally:
            await connection.close()
        self._initialized = True

    async def initialize(self) -> None:
        async with self._lock:
            await self._initialize_unlocked()

    async def _read_unlocked(self) -> AppSettings:
        await self._initialize_unlocked()
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                "SELECT payload FROM app_settings WHERE singleton = 1"
            )
            row = await cursor.fetchone()
            await cursor.close()
        finally:
            await connection.close()
        if row is None:
            raise RuntimeError("settings row is missing")
        return self._deserialize(str(row[0]))

    async def get(self) -> AppSettings:
        async with self._lock:
            return await self._read_unlocked()

    @staticmethod
    def _secret_text(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        return str(value)

    @classmethod
    def _drop_blank_secrets(cls, patch: MutableMapping[str, Any]) -> None:
        crowdnfo = patch.get("crowdnfo")
        if (
            isinstance(crowdnfo, MutableMapping)
            and "api_key" in crowdnfo
            and cls._secret_text(crowdnfo["api_key"]) == ""
        ):
            crowdnfo.pop("api_key")
        for connector_name in _CONNECTORS:
            connector = patch.get(connector_name)
            if not isinstance(connector, MutableMapping):
                continue
            for field in ("password", "api_key"):
                if field in connector and cls._secret_text(connector[field]) == "":
                    connector.pop(field)
        if (
            "application_api_token" in patch
            and cls._secret_text(patch["application_api_token"]) == ""
        ):
            patch.pop("application_api_token")

    @staticmethod
    def _merge(current: MutableMapping[str, Any], patch: dict[str, Any]) -> None:
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(current.get(key), MutableMapping):
                current[key].update(value)
            else:
                current[key] = value

    async def update(self, patch: SettingsPatch) -> AppSettings:
        async with self._lock:
            current = await self._read_unlocked()
            current_data = current.model_dump(mode="python")
            patch_data = patch.model_dump(exclude_unset=True, mode="python")
            self._drop_blank_secrets(patch_data)
            self._merge(current_data, patch_data)
            updated = AppSettings.model_validate(current_data)
            connection = await self._connect()
            try:
                await connection.execute(
                    """
                    UPDATE app_settings SET payload = ?, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (self._serialize(updated), datetime.now(UTC).isoformat()),
                )
                await connection.commit()
            finally:
                await connection.close()
            return updated

    async def public_view(self) -> dict[str, Any]:
        settings = await self.get()
        data = settings.model_dump(mode="json")
        data["crowdnfo"].pop("api_key", None)
        for connector_name in _CONNECTORS:
            data[connector_name].pop("password", None)
            data[connector_name].pop("api_key", None)
        data.pop("application_api_token", None)
        data["secrets_configured"] = {
            "crowdnfo_api_key": bool(settings.crowdnfo.api_key.get_secret_value()),
            "qbittorrent_password": bool(
                settings.qbittorrent.password.get_secret_value()
            ),
            "sabnzbd_api_key": bool(settings.sabnzbd.api_key.get_secret_value()),
            "application_api_token": bool(
                settings.application_api_token.get_secret_value()
            ),
        }
        return data

    async def update_public(self, patch: dict[str, Any]) -> dict[str, Any]:
        await self.update(SettingsPatch.model_validate(patch))
        return await self.public_view()

    async def close(self) -> None:
        """No-op kept for a uniform lifecycle API; connections are per-operation."""
