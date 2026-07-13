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

### Security

- Gate image and GitHub release publication on the reusable full CI workflow,
  including Python dependency auditing.
- Require a dedicated SABnzbd webhook secret, enforce a payload-size limit, and
  bound background action, hashing, polling, and health-check work.
- Remove recursive ownership changes from container startup and restrict
  automatic ownership repair to known state files under `/config`.
- Raise vulnerable `cryptography`, Black, and pytest dependency ranges to fixed
  releases.

### Known gaps

- CrowdNFO currently has no documented hash-only download lookup.
- CrowdNFO beta Swagger/auth and contributor routes may change; see
  `docs/crowdnfo-api.md`.
- YAML configuration is illustrative and is not automatically imported; the UI
  and SQLite are authoritative.
