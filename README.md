# Crowdarrr

Crowdarrr is a self-hosted, open-source CrowdNFO companion for qBittorrent,
SABnzbd, Radarr, and Sonarr. It finds release-specific NFO files that are missing
from downloads or libraries, writes them safely, and can contribute NFO,
MediaInfo, and file-list data back to CrowdNFO.

The main use case is a torrent stalled near 100% because its small `.nfo` file
has no available piece. Crowdarrr downloads the exact bytes, writes them at the
path qBittorrent expects, enables the file, forces a recheck, and confirms that
the torrent reached 100%. A failed verification is reported as an NFO mismatch;
media files are never modified or deleted.

## What it does

- **Repair / backfill:** scan qBittorrent and optional Radarr/Sonarr libraries,
  restore missing NFOs, and recheck repaired torrents.
- **Live-in:** process newly completed qBittorrent or SABnzbd downloads.
- **Live-out:** optionally upload NFOs, generated MediaInfo, and file lists.
- **Match safely:** cache streamed media SHA-256 values and fall back to an
  original release name when the current CrowdNFO API requires it.
- **Run as one service:** FastAPI serves both the API and compiled React UI on one
  port; SQLite stores settings, activity, jobs, counters, and hash metadata.

Every integration is optional. No private IP, local directory, tracker, category,
or credential is built into the application.

## Quick start with Docker Compose

Requirements: Docker Engine with Compose v2 and a local media directory that the
container may read and write.

```bash
cp .env.example .env
mkdir -p config media
docker compose -f docker-compose.example.yml up -d --build
```

Open `http://localhost:8000`, then add the CrowdNFO API key and only the
connectors you use under **Settings**. The API key is available from your
CrowdNFO profile.

The generic example mounts `./media` at `/data`. For an existing library, change
`MEDIA_ROOT` in `.env`, for example:

```dotenv
MEDIA_ROOT=/srv/media
PUID=1000
PGID=1000
TZ=Europe/Vienna
```

Use the UID and GID that own the host media files. The process drops privileges
to those IDs before FastAPI starts. On startup it owns only the configuration
directory and known SQLite state files; it does not recursively change ownership
below `/config` or `/data`. The container path is intentionally fixed at
`/config`; customize only the `CROWDARRR_CONFIG_ROOT` host-side bind source.

To use a published image instead of a local build, set
`CROWDARRR_IMAGE=ghcr.io/<owner>/crowdarrr:latest`. Compose retains the `build`
definition as a local fallback; `docker compose pull` uses the configured image.

## First-run checklist

1. Start in **dry-run** mode and leave contribution disabled.
2. Add the CrowdNFO URL and API key, choose **Save settings**, then use **Test**.
3. Add one connector at a time and verify its health.
4. Confirm every connector path maps to a path visible inside Crowdarrr.
5. Run **Scan & Repair now** and inspect activity before enabling scheduled
   backfill or contribution.
6. Dry-run actions are simulations. Disable **Dry run** and save settings before
   expecting files to be written or qBittorrent to recheck.

Settings are authoritative in SQLite and are managed through the UI.
[`config.example.yaml`](config.example.yaml) documents the schema but is not
currently imported at startup.

## Connector and path setup

| Connector | Minimum access | Path requirement |
| --- | --- | --- |
| CrowdNFO | Base URL and profile API key | None |
| qBittorrent | WebUI API; optional username/password | Content and save paths must map into `/data` |
| SABnzbd | API URL and API key | Completed-download paths must map into `/data` |
| Radarr | v3 API URL and API key | Movie file paths must map into `/data` |
| Sonarr | v3 API URL and API key | Episode file paths must map into `/data` |
| UmlautAdaptarr | Optional `/titlelookup?changedTitle=` endpoint | No file access; keep it private because the endpoint may have no auth |

The most reliable Docker layout mounts the same host media root at the same
container path in qBittorrent, SABnzbd, Radarr, Sonarr, and Crowdarrr. If that is
not possible, add a boundary-aware mapping such as remote `/downloads` to local
`/data/downloads`. Mappings are prefix mappings, not text replacement: `/data/a`
does not match `/data/archive`.

For qBittorrent, a repair candidate has an incomplete `.nfo`, incomplete torrent
progress, and essentially complete video content. Crowdarrr writes only the
expected NFO path, sets its priority to normal, requests a recheck, and waits for
completion. Username/password may be blank only when qBittorrent's own trusted
network authentication policy permits it.

Radarr and Sonarr may rename media, so scene/original names and optional
UmlautAdaptarr title recovery are important fallbacks. Library sidecars do not
need torrent-piece byte identity, but Crowdarrr still preserves the response
bytes and never overwrites a non-empty existing NFO.

The optional SABnzbd completion endpoint is `POST /api/webhooks/sabnzbd`. It is
disabled until `CROWDARRR_SAB_WEBHOOK_SECRET` is set and requires that value in
the `X-Crowdarrr-SAB-Secret` header. Send JSON containing `release_name` (or
`name`), `storage_path` (or `storage`), `category`, and SAB's `nzo_id`.
Crowdarrr verifies the ID and path against SAB history before processing it,
deduplicates completed events, and rejects bodies above
`CROWDARRR_SAB_WEBHOOK_MAX_BYTES` (64 KiB by default).

## Operating modes

`download_mode` has three values:

- `off`: no automatic download-side NFO fetches.
- `new_only`: process newly completed downloads.
- `new_and_backfill`: process new completions and scheduled/manual scans.

Contribution is independently disabled by default. NFO, MediaInfo, and file-list
uploads can each be selected. Hashing is streamed with a size limit and bounded
concurrency; the `(path, size, mtime) -> SHA-256` result is cached in SQLite.

Runtime work is bounded independently from UI settings. The defaults are two
concurrent actions with 64 waiting jobs, two concurrent hashes, a 30-second
qBittorrent completion poll, and a five-second per-connector health timeout.
Override these with `CROWDARRR_ACTION_MAX_CONCURRENCY`,
`CROWDARRR_ACTION_MAX_PENDING`, `CROWDARRR_HASH_MAX_CONCURRENCY`,
`CROWDARRR_QBIT_POLL_INTERVAL_SECONDS`, and
`CROWDARRR_HEALTHCHECK_TIMEOUT_SECONDS`. Values must be positive integers;
invalid values are logged and replaced with their defaults.

## Byte-exact repair and safety

Torrent repair treats an NFO as opaque bytes. It never decodes and re-encodes the
CrowdNFO response, so CRLF, CP437, and arbitrary byte sequences survive. Writes
are atomic. A repair is successful only after qBittorrent reports the expected
NFO complete and the torrent at 100%.

Crowdarrr is idempotent and will not replace an existing complete NFO. Connector
failures are isolated and retryable. It never touches or deletes media content.
Use dry-run while validating permissions, mappings, and category rules.

## Network exposure and API token

Crowdarrr has no browser login screen. Keep it on a trusted LAN or put the entire
site behind an authenticated reverse proxy; do not expose an unauthenticated
instance directly to the internet.

`CROWDARRR_API_TOKEN` optionally protects application endpoints under `/api/*`
with a bearer token; `/api/health` remains intentionally unauthenticated for
container probes. The SPA does not store or prompt for the token. If it is set
while using the web UI, a trusted reverse proxy must inject
`Authorization: Bearer <token>` for same-origin API requests and separately
authenticate users. The token is therefore useful for API clients or proxy
injection, not transparent browser authentication.

API clients may instead set the write-only `application_api_token` through the
settings API; an environment token takes precedence. This takes effect
immediately, so configure proxy injection before enabling it. The browser form
deliberately does not expose this lockout-prone control.

Connector credentials are stored in SQLite and encrypted with a Fernet master
key. When `CROWDARRR_MASTER_KEY` is unset, Crowdarrr creates a mode-`0600` key in
the configuration directory. Back up that key with the database. Supplying the
key by environment is supported for managed deployments; never commit it. If a
managed key is required, generate it exactly like this:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

The result is a 44-character URL-safe base64 Fernet key. A value from
`secrets.token_urlsafe()` is not interchangeable. Crowdarrr validates the key at
startup and exits with an actionable error before accepting settings if it is
malformed or cannot decrypt the existing database.

## Persistence, backup, and restore

All persistent state is below the container's `/config` directory, including
`crowdarrr.sqlite3` and the locally generated encryption key. The packaged
entrypoint rejects a different `CROWDARRR_DATA_DIR` to prevent a configuration
mistake from changing ownership across a broad host bind mount.

For a consistent simple backup, stop the container, copy the whole host `config`
directory, then restart it:

```bash
docker compose -f docker-compose.example.yml stop crowdarrr
cp -a config "config.backup.$(date +%Y%m%d)"
docker compose -f docker-compose.example.yml start crowdarrr
```

Restore the SQLite database and its matching key together. A database containing
encrypted credentials is not useful without the original key. Media under
`/data` is not application state and is outside this backup.

## Updates and logs

```bash
docker compose -f docker-compose.example.yml pull
docker compose -f docker-compose.example.yml up -d --build
docker compose -f docker-compose.example.yml logs -f crowdarrr
```

Tagged releases publish multi-architecture `linux/amd64` and `linux/arm64`
images. `main`, `edge`, semantic-version, `latest`, and immutable `sha-*` tags are
generated by the release workflows as appropriate. Prefer a version or SHA tag
when reproducibility matters.

## Reference deployment

The requester's LXC/macvlan layout is preserved as an optional example, not as a
default. It uses the external `media` macvlan on `10.10.3.0/24`, mounts
`/home/ubuntu/media:/data`, and documents the qBittorrent/Radarr/Sonarr addresses
and categories separately. See [Reference deployment](docs/reference-deployment.md)
and [`docker-compose.macvlan.example.yml`](docker-compose.macvlan.example.yml).

No VPN is required by Crowdarrr itself: it makes small authenticated API calls to
CrowdNFO and LAN connector APIs, while download and tracker traffic remains in
qBittorrent/SABnzbd. Operators may still route traffic according to their own
network and tracker policy.

## Troubleshooting

- **Permission denied:** make `PUID`/`PGID` match the host owner and ensure the
  media mount is writable. Crowdarrr intentionally does not `chown /data`.
- **Connector is healthy but files are not found:** compare the exact path
  reported by the connector with the path inside the Crowdarrr container; add a
  path mapping or use identical mounts.
- **qBittorrent returns 403/login errors:** supply WebUI credentials or correct
  qBittorrent's trusted-subnet policy. Do not assume an auth whitelist applies
  across Docker networks.
- **Torrent stays below 100% after recheck:** the downloaded NFO likely does not
  match the torrent piece. The activity entry is marked `nfo mismatch`; Crowdarrr
  does not alter the media to force completion.
- **CrowdNFO match misses:** current downloads ultimately require an exact
  release name; enable scene-name/UmlautAdaptarr recovery and retry.
- **Repair button appears to do nothing:** check the amber dashboard banner. In
  dry-run mode the action is deliberately recorded as a simulation and neither
  writes an NFO nor asks qBittorrent to recheck. Disable **Dry run**, save, and
  retry when the mappings have been verified.
- **Settings container will not start after setting a master key:** use the exact
  output of `Fernet.generate_key()` shown above. Restore the key that belongs to
  the database; replacing it makes already-encrypted connector secrets unreadable.
- **Macvlan service is unreachable from the Docker host:** host-to-macvlan child
  isolation is expected. Access it from another LAN host or add a deliberate host
  macvlan shim.
- **Health check fails:** verify the configured container port and inspect startup
  logs. `/api/health` itself does not require the optional API token.

## Current CrowdNFO API gaps

CrowdNFO's API is beta and may change. The full current assumptions, including
exact routes, are documented in [CrowdNFO API contract](docs/crowdnfo-api.md).
Most importantly:

- The current public API has no documented hash-only release lookup. Crowdarrr
  caches SHA-256 data, but download lookup currently needs a release name.
- Crowdarrr sends the profile key as `X-Api-Key`; the generated Swagger security
  description/test flow has not consistently reflected that working header.
- Connector health validates the saved key with `GET /api/user/me`; a public 404
  from a release lookup is not treated as proof of authentication.
- `POST /api/releases` is an administrator operation, not the contributor upload
  endpoint suggested by older examples.
- Current contribution routes are
  `POST /api/releases/{release_name}/files` for multipart NFO/MediaInfo and
  `POST /api/releases/{release_name}/filelists` for file lists.
- The current best-file route selects one NFO and has no relative-path selector.
  Torrents with several missing NFO entries are repaired as one batch using that
  response and accepted only if qBittorrent verifies every entry; otherwise the
  batch remains a clear, retryable mismatch.

Endpoint strings are isolated in `backend/crowdnfo/endpoints.py` so a beta API
change is small and reviewable. Automated tests exercise mocked contracts, but
this repository build does **not** claim end-to-end verification against a live
CrowdNFO account or the requester's private services.

Primary references:

- [CrowdNFO](https://crowdnfo.net/)
- [CrowdNFO Swagger UI](https://crowdnfo.net/api/swagger/index.html)
- [qBittorrent reference client](https://github.com/wake134/crowdclient-qbittorrent)
- [SABnzbd reference client](https://github.com/pixelhunterX/crowdclient-sabnzbd)

## Development and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for the local toolchain, quality gates, and
pull-request workflow. Crowdarrr is released under the [MIT License](LICENSE).
