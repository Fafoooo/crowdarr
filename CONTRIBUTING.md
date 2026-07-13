# Contributing to crowdarr

Thanks for improving crowdarr. Keep changes focused, test behavior at service
boundaries, and preserve the safety guarantees around media paths and raw NFO
bytes.

## Local setup

Use Python 3.12, Node.js 22, and the MediaInfo CLI. Docker with Compose v2 is
needed for container verification.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
cd frontend
npm ci
cd ..
```

Run the backend from the repository root:

```bash
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

For frontend development, run Vite separately in another terminal:

```bash
cd frontend
npm run dev
```

The production image builds the SPA and serves it through FastAPI on the same
port.

## Required quality checks

Run the same gates as CI before opening a pull request:

```bash
ruff check .
black --check .
mypy backend
pytest --cov=backend --cov-report=term-missing
pip-audit

cd frontend
npm run lint
npm run format:check
npm run typecheck
npm run test:coverage
npm run build
cd ..

docker build -t crowdarr:dev .
```

Backend coverage is enforced at 80% or higher. Prefer deterministic unit and
service tests with mocked HTTP transports; do not require contributor-owned
CrowdNFO or *arr instances in CI.

## Design rules

- Treat downloaded NFO content as opaque bytes from HTTP response through atomic
  disk write. Do not decode it for torrent repair.
- Never modify or delete a media file.
- Validate connector paths after mapping and reject traversal or symlink escape.
- Keep CrowdNFO endpoint strings in `backend/crowdnfo/endpoints.py`.
- Do not retry non-idempotent uploads unless the API supplies an idempotency
  contract.
- Keep connectors optional and isolate their failures.
- Do not return persisted connector secrets to the browser.
- Add UI-configurable behavior to the persisted settings model; avoid requiring
  hand-edited configuration files.

## Pull requests

Include a concise problem statement, the behavior change, tests, and any operator
migration or security impact. Update README, examples, and the changelog when the
public behavior changes. Avoid unrelated formatting churn.

Never commit API keys, service credentials, database files, media samples from a
private library, or a generated Fernet key. Use obvious placeholders in examples.
Review the complete diff before publishing a branch.

For a suspected vulnerability, use GitHub private vulnerability reporting when
it is enabled instead of opening a public exploit report.

## Releases

Version tags use `vMAJOR.MINOR.PATCH`. Pushing a matching tag creates release
notes and publishes multi-architecture GHCR images only after the reusable full
CI workflow succeeds for that tag. Maintainers should update `CHANGELOG.md`
before tagging and verify CI on the exact commit.
