# Python SDK and CLI

RelayCore includes a typed, synchronous Python client and an API-backed `relaycore` command. Both use expiring, workspace-bound API tokens and the same permission checks as dashboard sessions.

## Create a token

Sign in through the RelayCore dashboard, choose a workspace where you are an owner or administrator, and use **API access** to create a token. Choose a permission level and expiry from 1 to 90 days, then copy the secret when it appears; RelayCore stores only its SHA-256 hash and will not show it again. The default `operator` ceiling permits workflow operations without workspace administration. `viewer` is read-only; `admin` must be chosen explicitly. The effective role is capped by the user's current workspace membership, so role changes and membership removal take effect immediately. Revoke a token from the same dashboard panel.

Tokens are workspace-bound. Fine-grained per-action scopes are not implemented; the role ceiling uses RelayCore's existing viewer/operator/admin permission groups. Keep the token in a secret manager or process environment, not in source control or command history. The client requires HTTPS outside loopback.

## Install from a checkout

The package has no runtime dependencies beyond the Python standard library. From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
$env:RELAYCORE_URL = "https://relay.example.com"
$env:RELAYCORE_WORKSPACE_ID = "<workspace-id>"
$env:RELAYCORE_API_TOKEN = "<token-from-dashboard>"
relaycore workspaces list
relaycore workflow list
relaycore runs list
```

For local development, `http://127.0.0.1:8000` is allowed. Non-loopback HTTP URLs are rejected by the client.

Create a versioned production workflow from a JSON file and start it:

```json
{
  "title": "Notify the release channel",
  "steps": [
    {
      "name": "Post release update",
      "action": "slack_message",
      "payload": {"channel": "C0123456789", "text": "Release is ready."}
    }
  ]
}
```

```powershell
relaycore workflow create .\workflow.json --idempotency-key release:v1
relaycore workflow run <workflow-id> --idempotency-key release:2026-10-08
relaycore workflow show <workflow-id>
relaycore runs show <run-id>
relaycore runs cancel <run-id>
```

The example needs a connected Slack app and a channel the bot can access. Production workflows accept only the actions and payloads documented in the API. Use a stable idempotency key when retrying a create or start after a connection failure; the SDK does not automatically retry writes.

## Python

```python
from relaycore_sdk import RelayCoreClient

client = RelayCoreClient(
    "https://relay.example.com",
    token="<token-from-secret-manager>",
    workspace_id="<workspace-id>",
)

for workflow in client.list_workflow_definitions():
    print(workflow.id, workflow.title, workflow.current_version)

run = client.start_workflow("<workflow-id>", idempotency_key="release:2026-10-08")
print(run.id, run.status)
```

The public Python types are available from `relaycore_sdk`; the distribution includes a `py.typed` marker. Install from the project checkout with `pip install -e .`. A package-index release is not published yet.
