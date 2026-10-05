# Testing and verification

The integration suite requires PostgreSQL 18 with UTF-8. Set `RELAYCORE_TEST_DATABASE_URL` to a disposable database, or use `scripts/test.ps1` with Docker Compose. The tests create isolated tenants and temporary schemas; never point them at production data.

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests benchmarks scripts
.\.venv\Scripts\python.exe -m pre_commit run --all-files
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q
.\.venv\Scripts\python.exe -m compileall -q app tests benchmarks scripts
```

Current tests cover task state and retry behavior, lease/process recovery, concurrent idempotency and queue admission, tenant boundaries, workflow version pinning, session expiration/revocation, workspace RBAC, CSRF origin checks, signed webhook intake and Demo Mode trigger matching, HTTP allowlist/DNS pinning/credential-host binding, bounded response handling and status retry classification, fresh and upgrade migrations, and measured query plans. The current report is 79% app source coverage with no threshold. HTTP transport tests use a no-network adapter; they do not verify a real third-party endpoint. The browser script is syntax-checked with `node --check` when changed. CI runs Ruff, the local pre-commit hook, and the PostgreSQL test suite; static type checking, coverage thresholds, provider contract tests, and browser end-to-end tests are not configured.

Local performance evidence is synthetic and machine-specific. See [BENCHMARKS.md](BENCHMARKS.md); it is not a production capacity claim.
