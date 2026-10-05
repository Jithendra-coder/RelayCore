# ADR 010: Production custom-webhook workflow triggers

## Status

Accepted for the first production event-driven path.

## Decision

- In the signed webhook transaction, match only active workflow versions with the exact endpoint ID and JSON `type`. Production matches must contain only the constrained HTTP action and valid workspace/host-bound credentials.
- Admit the incoming event and its triggered run atomically. `workflow_runs.trigger_event_id` references the durable event row; the run also pins its immutable workflow version and definition hash. A duplicate endpoint/event key creates no additional run.
- Allow an exact `{"$event":"/json/pointer"}` object to select values from the event payload inside an HTTP JSON body. URLs, headers, and HTTP response-to-next-step mapping remain static. Missing pointers and rendered bodies over 4 KiB are permanent action failures.
- Keep event payloads out of run history and audit responses. Store only event ID, event type, key, hash, and receive time in run metadata.

## Consequences and limits

- A run can resume after worker restart with its original source event and version. Current webhook rows are retained indefinitely because there is no retention policy yet.
- Selecting an event field explicitly sends that data to the credential's one bound external host. Workspace administrators are responsible for reviewing mappings and data destination.
- Delivery remains at least once. The destination must honor RelayCore's idempotency key. No GitHub/Slack provider-native trigger, schema validation, response mapping, or external endpoint interoperability is included in this slice.
