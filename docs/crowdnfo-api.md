# CrowdNFO beta API contract

Crowdarrr deliberately keeps every CrowdNFO route and auth header in
`backend/crowdnfo/endpoints.py`. CrowdNFO is beta; this document records the
contract implemented at the time of the initial release and should be checked
before changing that module.

## Implemented routes

| Purpose | Method and route | Payload |
| --- | --- | --- |
| Validate profile key | `GET /api/user/me` | Authenticated profile response |
| Select best NFO | `GET /api/releases/{release_name}/files/best` | Query: `type=NFO`, `raw=false`, `fallback=false` |
| Download bytes | `GET /api/files/{file_id}/download` | Response body is consumed as raw bytes |
| Upload NFO/MediaInfo | `POST /api/releases/{release_name}/files` | Multipart `File` plus `FileType`, `OriginalFileName`, `Category`, optional `FileHash` |
| Upload file list | `POST /api/releases/{release_name}/filelists` | JSON release/category/entries plus optional media hash |

Path segments are percent-encoded. The base URL must be an absolute HTTP(S)
service root. A single trailing `/api` entered in Settings is accepted and
canonicalized to the root, preventing `/api/api/...` ambiguity. API keys are sent
as `X-Api-Key`. GET requests are bounded, rate-limited, and retried for 429/5xx
responses; uploads are not retried automatically because their idempotency cannot
be assumed.

## Byte preservation

The best-file call returns a `fileId`; Crowdarrr then downloads from the file
endpoint and returns `response.content`. No text decoding, newline normalization,
or character-set conversion occurs. This is mandatory for qBittorrent piece
verification.

## Known gaps and deliberate behavior

1. **No hash-only query:** release details may contain media hashes, and uploads
   accept `FileHash`, but the current public contract has no documented endpoint
   that starts with a media SHA-256 alone. A hash-only lookup fails locally with
   an actionable unsupported-operation error; the matching layer may then use an
   exact recovered release name.
2. **Swagger auth mismatch:** the generated Swagger security description and
   interactive test flow have not consistently represented the working
   `X-Api-Key` profile-key header. Crowdarrr uses the header observed in the
   current contract/reference behavior and keeps it configurable in one module.
3. **Release creation is not contribution:** `POST /api/releases` is restricted
   to administrators. A normal contributor uploads to the nested `files` and
   `filelists` routes listed above; Crowdarrr does not attempt release creation.
4. **Beta stability:** response shapes and endpoint paths can change. A missing
   `fileId` is treated as a contract error, not guessed. Unknown download
   endpoints are never tried because an incorrect NFO is worse than a clear miss.
5. **One best NFO response:** the current lookup has no relative-path selector.
   When a torrent reports several missing NFO entries, Crowdarrr places the one
   best-file response as a batch and reports success only if qBittorrent verifies
   every entry. A partial or failed verification is retained as a retryable,
   explicitly logged mismatch.

These are implementation assumptions, not a claim that live integration was
verified with a production account during repository creation. Contract tests use
mocked HTTP responses.

## Sources

- [CrowdNFO Swagger UI](https://crowdnfo.net/api/swagger/index.html)
- [CrowdNFO home](https://crowdnfo.net/)
- [wake134/crowdclient-qbittorrent](https://github.com/wake134/crowdclient-qbittorrent)
- [pixelhunterX/crowdclient-sabnzbd](https://github.com/pixelhunterX/crowdclient-sabnzbd)
