# Testing and verification

The integration suite requires PostgreSQL 18 with UTF-8. Set `RELAYCORE_TEST_DATABASE_URL` to a disposable database, or use `scripts/test.ps1` with Docker Compose. The tests create isolated tenants and temporary schemas; never point them at production data.

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests benchmarks scripts
.\.venv\Scripts\python.exe -m pre_commit run --all-files
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q
.\.venv\Scripts\python.exe -m compileall -q app tests benchmarks scripts
```

Current tests cover task state and retry behavior, lease/process recovery, standalone worker identity and execution, concurrent idempotency and queue admission, tenant boundaries, workflow version pinning, session expiration/revocation, workspace RBAC, CSRF origin checks, signed webhook intake and matching Demo/production triggers, HTTP allowlist/DNS pinning/credential-host binding, body event-reference validation/resolution and size limits, sandbox-action rejection in production workers, bounded response handling and status retry classification, fresh and upgrade migrations, measured query plans, GitHub OAuth/webhook handling, and Slack OAuth/events/message actions including rate-limit delays. Trigger tests verify event binding, duplicate delivery, payload-free run history, and version pinning. Provider transport tests use mocked responses; they do not verify a real third-party endpoint. The latest run was 64 passed with 79% app source coverage on PostgreSQL 18. The browser script is syntax-checked with `node --check` when changed. CI runs Ruff, the local pre-commit hook, and the PostgreSQL test suite; static type checking, coverage thresholds, provider contract tests, and browser end-to-end tests are not configured.

Local performance evidence is synthetic and machine-specific. See [BENCHMARKS.md](BENCHMARKS.md); it is not a production capacity claim.
