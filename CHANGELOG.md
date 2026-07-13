# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.3] - 2026-07-13

### Added

- Persist per-torrent repair outcomes and surface clear inline states for fixed,
  unavailable, transient, pending, and non-NFO-related downloads.
- Add a configurable recheck timeout, a durable `placed` counter, delayed repair
  reconciliation, and a dashboard funnel from match through verified repair.
- Cache definitive CrowdNFO misses for 12 hours so repeated repair attempts do not
  re-query the service or inflate lifetime counters.

### Fixed

- Include release names and actionable reasons in repair, lookup, and contribution
  activity; keep transient CrowdNFO failures retryable instead of counting them as
  permanent misses.
- Replace the incompatible `/api/user/me` profile-key test with an honest release
  endpoint probe that distinguishes invalid credentials from an API that is merely
  reachable but cannot verify the key.
- Raise the qBittorrent recheck default from 5 to 30 minutes and reconcile torrents
  that finish after the foreground job returns.
- Bound and pace CrowdNFO traffic while retrying connection errors, rate limits,
  and server failures with backoff.
- Improve connector states, repair-readiness badges, long-name handling, responsive
  torrent cards, loading states, and dashboard KPI readability.
- Standardize new user-facing names and environment variables on `crowdarr` while
  retaining legacy `CROWDARRR_*`, `crowdarrr.sqlite3`, SAB hook header, and GHCR
  image compatibility for existing installations.
- Preserve legacy-only environment overrides for direct `docker run` deployments
  and retain their numeric PUID/PGID ownership while renaming the image user.

## [0.1.2] - 2026-07-13

### Added

- Show every incomplete qBittorrent download on the dashboard with a precise
  repair-readiness reason and expose **Repair** only for valid missing-NFO
  candidates.
- Record live qBittorrent fetch and contribution outcomes in activity and counters
  with independent idempotency for each action.

### Fixed

- Test CrowdNFO and all optional connectors against the current unsaved form
  values, keep draft secrets available during the test, and preserve the
  write-only configured-secret state after saving.
- Allow Radarr and Sonarr connection health tests before path mappings are added;
  mappings remain mandatory for library scans.
- Count successful CrowdNFO downloads and matches independently from subsequent
  torrent verification, while reserving misses for lookup failures and repaired
  for verified 100% torrents.
- Migrate existing activity-derived dashboard counters once so verification
  timeouts and NFO mismatches are no longer reported as CrowdNFO misses.
- Continue incomplete-torrent discovery when qBittorrent cannot return the file
  list for one torrent, and group multiple missing NFO paths into one dashboard
  row.
- Prevent the dashboard action column from clipping at desktop widths and keep the
  table contained on mobile layouts.

## [0.1.1] - 2026-07-13

### Added

- Expose repair job status and make the dashboard follow each requested repair
  through to its real success or failure result.

### Fixed

- Validate the settings encryption key at startup, fail safely when stored secrets
  cannot be decrypted, and document how to generate a valid Fernet key.
- Canonicalize CrowdNFO base URLs, reject unsupported URL paths, and use the
  authenticated profile endpoint for connector health so an unconfigured API key
  can no longer appear connected.
- Test connectors only with saved settings and report prerequisites such as a
  missing API key, a disabled connector, or unsaved changes directly in the UI.
- Make dry-run mode explicit on the dashboard and distinguish simulated scans and
  repairs from live actions.

## [0.1.0] - 2026-07-13

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
- Preserve an NFO that another process replaces while qBittorrent is rechecking;
  mismatch cleanup removes only the exact payload crowdarr placed.
- Raise vulnerable `cryptography`, Black, and pytest dependency ranges to fixed
  releases.

### Known gaps

- CrowdNFO currently has no documented hash-only download lookup.
- CrowdNFO beta Swagger/auth and contributor routes may change; see
  `docs/crowdnfo-api.md`.
- YAML configuration is illustrative and is not automatically imported; the UI
  and SQLite are authoritative.

[Unreleased]: https://github.com/Fafoooo/crowdarr/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/Fafoooo/crowdarr/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Fafoooo/crowdarr/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Fafoooo/crowdarr/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Fafoooo/crowdarr/releases/tag/v0.1.0
