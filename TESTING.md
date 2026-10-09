# Testing and verification

The integration suite requires PostgreSQL 18 with UTF-8. Set `RELAYCORE_TEST_DATABASE_URL` to a disposable database, or use `scripts/test.ps1` with Docker Compose. The tests create isolated tenants and temporary schemas; never point them at production data.

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests benchmarks scripts sdk
.\.venv\Scripts\python.exe -m pre_commit run --all-files
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q
.\.venv\Scripts\python.exe -m compileall -q app tests benchmarks scripts sdk
```

The dashboard-to-worker browser smoke requires Docker and Chromium. Install Chromium once, then run:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m scripts.compose_smoke
```

Current tests cover task state and retry behavior, lease/process recovery, standalone worker identity and execution, concurrent idempotency and queue admission, tenant boundaries, workflow version pinning, session expiration/revocation, workspace RBAC, CSRF origin checks, signed webhook intake and matching Demo/production triggers, live SSE session/token/membership revocation, HTTP allowlist/pinned DNS/credential-host binding, DNS timeout before connection, cumulative DNS-plus-HTTPS action deadlines capped at half the lease, body event-reference validation/resolution and size limits, sandbox-action rejection in production workers, bounded response handling and status retry classification, fresh and upgrade migrations, measured query plans, GitHub OAuth/webhook handling, Slack OAuth/events/message actions with pinned DNS and rate-limit delays, encryption-key rewrapping with fingerprint and rollback checks, schedule API authorization/lifecycle/idempotency, and API-token/SDK/CLI behavior. Token tests cover hash-only storage, expiry, revocation, workspace binding, role ceilings, current membership caps, session-only issuance, and rate limiting. SDK tests cover HTTPS policy, error handling, request idempotency, and redirects. Provider transport tests use mocked responses; they do not verify a real third-party endpoint. CI runs on PostgreSQL 18 and enforces an 80% coverage floor, along with Ruff, Mypy, pre-commit, both Compose validations, Prometheus checks, and an image build. The dashboard script is syntax-checked with `node --check`; tests assert API-unavailable guidance and schedule panel/source controls. CI runs a Chromium smoke through the dashboard to a separate worker using the disposable Compose stack. It covers Demo Mode and keyboard activation; production OIDC, provider integrations, and schedule-management UI are not browser-tested.

Local performance evidence is synthetic and machine-specific. See [BENCHMARKS.md](BENCHMARKS.md); it is not a production capacity claim.
