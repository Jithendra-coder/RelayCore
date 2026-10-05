# Current limitations

RelayCore is an actively developed prototype. It is not a production workflow product yet.

- OIDC and workspace code is locally tested, but no live identity-provider flow was verified. Email invitations, MFA, recovery, and user account administration are missing.
- GitHub/Slack OAuth and webhooks, provider actions, generic outbound HTTP actions, and production workflow actions are missing. Encrypted workspace bearer credentials can be managed through the API, but no adapter consumes them yet. Custom webhooks can be durably accepted and exposed as metadata; exact-match trigger execution is enabled only in Demo Mode, where sandbox actions run.
- Custom webhook signing secrets and workspace bearer credentials are Fernet-encrypted with a required environment-injected key. Key versioning and automated master-key rotation are absent; provider-side token revocation is not implemented.
- Workflow definitions support only the local sandbox action allow-list. Durable schedules, conditional branches, waits, approvals, and compensation are missing.
- PostgreSQL is the durable queue. It is sufficient for the measured local workload, but independent broker replay/consumer scaling has not been tested. The per-tenant event-ordering lock can become a write bottleneck.
- The API and its supervisor own worker processes. Multiple API replicas with duplicate worker IDs are unsafe; production needs a separate worker service with unique identities.
- Real-time progress uses polling-backed SSE over PostgreSQL. There is no independent real-time gateway or durable notification outbox.
- Incoming webhook raw payloads have no automatic retention/cleanup and no per-endpoint JSON schema validation. Apply edge IP/network limits before exposing public ingress broadly.
- Metrics are limited to tenant-scoped Prometheus values and dashboard aggregates. OpenTelemetry traces, pool metrics, Grafana dashboards, alert rules, and provider metrics are absent.
- CI runs Ruff, pre-commit, and tests with coverage reporting, but type checking, coverage thresholds, automated dependency/security scanning, staging, deployment rollback, and cloud production are not implemented.
- The SDK, CLI, complete product UI, final evidence pack, case study, and demo video are not implemented.
- Current load and query numbers are local synthetic evidence only. See [BENCHMARKS.md](BENCHMARKS.md).
