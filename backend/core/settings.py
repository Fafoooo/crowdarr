"""Validated, generic runtime settings models."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

from backend.core.files import MismatchCleanupPolicy


def _empty_secret() -> SecretStr:
    return SecretStr("")


def _canonical_crowdnfo_base_url(value: object) -> object:
    if value is None:
        return value
    parsed = urlsplit(str(value).strip())
    if parsed.username or parsed.password:
        raise ValueError("CrowdNFO base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("CrowdNFO base_url must not contain query or fragment")
    if parsed.path.rstrip("/") not in {"", "/api"}:
        raise ValueError("CrowdNFO base_url must be the service root")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class DownloadMode(StrEnum):
    OFF = "off"
    NEW_ONLY = "new_only"
    NEW_AND_BACKFILL = "new_and_backfill"


class CrowdNFOSettings(StrictModel):
    base_url: AnyHttpUrl = "https://crowdnfo.net"  # type: ignore[assignment]
    api_key: SecretStr = Field(default_factory=_empty_secret)

    @field_validator("base_url", mode="before")
    @classmethod
    def canonicalize_base_url(cls, value: object) -> object:
        return _canonical_crowdnfo_base_url(value)


class CrowdNFOPatch(StrictModel):
    base_url: AnyHttpUrl | None = None
    api_key: SecretStr | None = None

    @field_validator("base_url", mode="before")
    @classmethod
    def canonicalize_base_url(cls, value: object) -> object:
        return _canonical_crowdnfo_base_url(value)


class ConnectorSettings(StrictModel):
    enabled: bool = False
    base_url: AnyHttpUrl | None = None
    username: str | None = None
    password: SecretStr = Field(default_factory=_empty_secret)
    api_key: SecretStr = Field(default_factory=_empty_secret)


class ConnectorPatch(StrictModel):
    enabled: bool | None = None
    base_url: AnyHttpUrl | None = None
    username: str | None = None
    password: SecretStr | None = None
    api_key: SecretStr | None = None


class ContributionSettings(StrictModel):
    enabled: bool = False
    nfo: bool = True
    mediainfo: bool = True
    filelist: bool = True


class ContributionPatch(StrictModel):
    enabled: bool | None = None
    nfo: bool | None = None
    mediainfo: bool | None = None
    filelist: bool | None = None


class PathMappingSetting(StrictModel):
    remote_root: str
    local_root: str

    @field_validator("remote_root")
    @classmethod
    def validate_remote_root(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("remote_root must be an absolute POSIX path")
        return str(path)

    @field_validator("local_root")
    @classmethod
    def validate_local_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("local_root must be an absolute safe path")
        return str(path)


class AppSettings(StrictModel):
    crowdnfo: CrowdNFOSettings = Field(default_factory=CrowdNFOSettings)
    qbittorrent: ConnectorSettings = Field(default_factory=ConnectorSettings)
    sabnzbd: ConnectorSettings = Field(default_factory=ConnectorSettings)
    radarr: ConnectorSettings = Field(default_factory=ConnectorSettings)
    sonarr: ConnectorSettings = Field(default_factory=ConnectorSettings)
    umlautadaptarr: ConnectorSettings = Field(default_factory=ConnectorSettings)
    download_mode: DownloadMode = DownloadMode.OFF
    auto_recheck: bool = True
    nfo_mismatch_policy: MismatchCleanupPolicy = MismatchCleanupPolicy.KEEP
    contribute: ContributionSettings = Field(default_factory=ContributionSettings)
    match_strategy: Literal[
        "hash_then_release_name", "hash_only", "release_name_only"
    ] = "hash_then_release_name"
    hash_max_size_bytes: int = Field(default=64 * 1024**3, gt=0)
    path_mappings: list[PathMappingSetting] = Field(default_factory=list)
    category_mappings: dict[str, str] = Field(default_factory=dict)
    backfill_cron: str = "0 3 * * *"
    dry_run: bool = True
    application_api_token: SecretStr = Field(default_factory=_empty_secret)

    @field_validator("backfill_cron")
    @classmethod
    def validate_backfill_cron(cls, value: str) -> str:
        return validate_cron(value)


class SettingsPatch(StrictModel):
    crowdnfo: CrowdNFOPatch | None = None
    qbittorrent: ConnectorPatch | None = None
    sabnzbd: ConnectorPatch | None = None
    radarr: ConnectorPatch | None = None
    sonarr: ConnectorPatch | None = None
    umlautadaptarr: ConnectorPatch | None = None
    download_mode: DownloadMode | None = None
    auto_recheck: bool | None = None
    nfo_mismatch_policy: MismatchCleanupPolicy | None = None
    contribute: ContributionPatch | None = None
    match_strategy: (
        Literal["hash_then_release_name", "hash_only", "release_name_only"] | None
    ) = None
    hash_max_size_bytes: int | None = Field(default=None, gt=0)
    path_mappings: list[PathMappingSetting] | None = None
    category_mappings: dict[str, str] | None = None
    backfill_cron: str | None = None
    dry_run: bool | None = None
    application_api_token: SecretStr | None = None

    @field_validator("backfill_cron")
    @classmethod
    def validate_optional_backfill_cron(cls, value: str | None) -> str | None:
        return validate_cron(value) if value is not None else None


def validate_cron(value: str) -> str:
    try:
        CronTrigger.from_crontab(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("backfill_cron must be a valid five-field cron") from exc
    return value
