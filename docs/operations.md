# Operations

## Prometheus metrics and alerts

`/metrics` reports one workspace at a time. It requires a signed-in workspace session or a workspace metrics token. Metrics tokens can read only `/metrics`; they cannot call workflow or workspace APIs. The database stores only a SHA-256 hash of each randomly generated token. The plaintext is returned once at creation, and an owner or admin can list token metadata and revoke a token through the workspace API:

```text
POST   /api/workspaces/{workspace_id}/metrics-tokens  {"name":"Prometheus production"}
GET    /api/workspaces/{workspace_id}/metrics-tokens
DELETE /api/workspaces/{workspace_id}/metrics-tokens/{token_id}
```

Copy the one-time `token` from the POST response to a file readable only by Prometheus at `/etc/prometheus/secrets/relaycore-metrics-token`. The file contains the token alone, without the `Bearer` prefix. Configure a separate scrape job and token for every monitored workspace, and give each job a static `workspace` label for alert identification. To rotate, create a replacement, update and verify the scrape, then revoke the previous token. Keep tokens out of source control and revoke them when a scraper is removed or a credential is compromised.

The example files in `monitoring/` assume Prometheus shares a container network with the Compose API service (`api:8000`). Mount `prometheus.yml`, `relaycore.rules.yml`, and the token file at the paths shown in the config. Add a separate job and secret for each workspace. The included rules are starting thresholds: runnable-task age above five minutes for five minutes, an expired lease persisting two minutes, or unreplayed dead letters persisting two minutes. CI runs `promtool check` and `promtool test rules` against them, following the [Prometheus rule-testing format](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/). Prometheus evaluates and displays these rules; configure Alertmanager separately to route notifications to the team's chosen destination.

The endpoint does not attach workspace IDs to metrics, so labels must be applied by the scrape configuration. The gauges describe only the workspace selected by that scrape token; they are not fleet-wide aggregates. Collector-backed tracing export, pool metrics, and live alert delivery have not been verified in a deployed environment.
