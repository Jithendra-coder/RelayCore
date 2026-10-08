# Testing and verification

The integration suite requires PostgreSQL 18 with UTF-8. Set `RELAYCORE_TEST_DATABASE_URL` to a disposable database, or use `scripts/test.ps1` with Docker Compose. The tests create isolated tenants and temporary schemas; never point them at production data.

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests benchmarks scripts sdk
.\.venv\Scripts\python.exe -m pre_commit run --all-files
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q
.\.venv\Scripts\python.exe -m compileall -q app tests benchmarks scripts sdk
```

Current tests cover task state and retry behavior, lease/process recovery, standalone worker identity and execution, concurrent idempotency and queue admission, tenant boundaries, workflow version pinning, session expiration/revocation, workspace RBAC, CSRF origin checks, signed webhook intake and matching Demo/production triggers, HTTP allowlist/DNS pinning/credential-host binding, body event-reference validation/resolution and size limits, sandbox-action rejection in production workers, bounded response handling and status retry classification, fresh and upgrade migrations, measured query plans, GitHub OAuth/webhook handling, Slack OAuth/events/message actions including rate-limit delays, encryption-key rewrapping with fingerprint and rollback checks, and API-token/SDK/CLI behavior. Token tests cover hash-only storage, expiry, revocation, current role/membership, rate limiting, and workspace scope. SDK tests cover HTTPS policy, error handling, request idempotency, and redirects. Provider transport tests use mocked responses; they do not verify a real third-party endpoint. The current full local run passed 106 tests with 81.51% app source coverage on PostgreSQL 17.9. CI runs on PostgreSQL 18 and enforces an 80% coverage floor, along with Ruff, pre-commit, both Compose validations, Prometheus checks, and an image build. The dashboard script is syntax-checked with `node --check`. Static type checking, provider contract tests, and browser end-to-end tests are not configured.

Local performance evidence is synthetic and machine-specific. See [BENCHMARKS.md](BENCHMARKS.md); it is not a production capacity claim.
