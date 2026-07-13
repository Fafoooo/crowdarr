# Crowdarrr implementation plan

## Product boundary

Crowdarrr is a single-port, self-hosted companion for CrowdNFO. Every connector,
path, category, schedule, credential, and operating mode is optional and stored
through the web UI. The deployment described by the original requester is an
example profile only; no private host, subnet, path, or credential is compiled
into the application.

## Architecture

- **Runtime:** FastAPI owns the REST API, lifespan, scheduler, SQLite database,
  and the compiled React SPA. Long-running scans execute as bounded async jobs.
- **Domain core:** connector-neutral discovery records feed a matching service
  (cached SHA-256 first, release name second), then a repair/contribution
  service. Atomic byte writes and explicit post-recheck verification protect
  torrent correctness.
- **Adapters:** CrowdNFO endpoint names live in one beta-API contract module.
  qBittorrent, SABnzbd, Radarr, Sonarr, and UmlautAdaptarr adapters expose small,
  mockable async interfaces and degrade independently.
- **Persistence:** SQLModel/SQLite stores settings, encrypted-at-rest connector
  secrets when a master key is supplied, activity, counters, retryable misses,
  jobs, and the `(path, size, mtime) -> sha256` cache.
- **UI:** a dark-first React/Vite/Tailwind SPA provides dashboard, stuck-item
  repair, activity/log viewing, complete settings, connection tests, and manual
  scans. Secrets are write-only after persistence.
- **Packaging:** a multi-stage image builds the SPA and Python environment,
  bundles MediaInfo, drops privileges through a PUID/PGID entrypoint, and serves
  one configurable port. Compose includes a generic bridge profile and a
  documented opt-in macvlan example.

## Milestones

1. Lock down behavior with tests and confirm the CrowdNFO beta contract from its
   Swagger/reference clients.
2. Build the qBittorrent missing-NFO detector and byte-exact repair/recheck loop.
3. Add persistence, matching/hash cache, settings, jobs, scheduler, and API.
4. Build dashboard/settings UI and connector health/actions.
5. Add SABnzbd, Radarr/Sonarr, UmlautAdaptarr, and contribution flows.
6. Add container/runtime packaging, examples, CI/CD, release automation, and
   operator/developer documentation.
7. Run coverage, lint, formatting, types, dependency/security, Docker, and
   architecture-graph verification.

## Decisions and open API questions

- CrowdNFO is beta. Exact paths and parameter names will be isolated in
  `backend/crowdnfo/endpoints.py`; unsupported download behavior will fail with
  an actionable error rather than guessing or transforming bytes.
- Hashing is streamed and globally bounded. Files above the configured hashing
  limit fall back to release-name matching rather than being partially hashed.
- Connector-reported paths must map into configured allowed media roots. Path
  traversal, symlink escape, and writes outside those roots are rejected.
- qBittorrent success means the expected NFO file becomes complete and the
  torrent reaches `progress == 1` after recheck. A timeout remains retryable; a
  completed recheck below 100% is recorded as an NFO mismatch.
- Library sidecars use the raw CrowdNFO payload too, although byte identity is
  only required for torrent pieces. Existing non-empty NFOs are never replaced.
- Live-in initially uses a poller plus optional SAB post-processing webhook;
  native push support can be added later without changing the domain service.
- Public deployments are assumed to sit behind a trusted reverse proxy or LAN.
  Optional application API-token protection will be documented for exposed
  installations; CrowdNFO/connector secrets are never returned to the browser.
