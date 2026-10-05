# Benchmarks

## Current measured evidence

All numbers below are **MEASURED** in a local synthetic PostgreSQL 18 workload, not an estimate of production capacity. The full run conditions and machine limitations are in [docs/evidence.md](docs/evidence.md) and the raw result is in `benchmarks/results/local-run.json`.

- 100 workflows: P95 3,096.99 ms with 1 worker, 2,364.86 ms with 2, and 2,289.27 ms with 4; 0/100 failures in each run.
- Queue-shaped 20,000-row query: 15.411 ms before a partial index and 0.126 ms after in one local run.
- Failure recovery includes lease expiry, replacement-worker processing, database restart, and DLQ replay; it does not prove multi-node failover or network-partition behavior.

Re-run with `.\.venv\Scripts\python.exe -m benchmarks.run --workflows 100` against an isolated UTF-8 PostgreSQL database. The checked-in numbers are historical local evidence; record hardware, database version, command, and raw output for any new run. 8/16-worker tests, external webhook throughput, P50/P99 under provider latency, and resource measurements are not yet available.
