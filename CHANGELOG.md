# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial FastAPI, SQLite, scheduler, connector, matching, repair, contribution,
  and React web UI implementation.
- Byte-exact qBittorrent NFO repair with post-recheck verification.
- Optional SABnzbd, Radarr, Sonarr, and UmlautAdaptarr integrations.
- Single-container multi-stage build with MediaInfo and PUID/PGID privilege drop.
- Generic Compose deployment and opt-in LXC/macvlan reference profile.
- CI, multi-architecture GHCR publishing, GitHub releases, and Dependabot.

### Known gaps

- CrowdNFO currently has no documented hash-only download lookup.
- CrowdNFO beta Swagger/auth and contributor routes may change; see
  `docs/crowdnfo-api.md`.
- YAML configuration is illustrative and is not automatically imported; the UI
  and SQLite are authoritative.
