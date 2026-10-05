$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -3.13 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$env:PATH = (Join-Path $projectRoot '.venv\Scripts') + [IO.Path]::PathSeparator + $env:PATH
& .\.venv\Scripts\python.exe -m ruff check app tests benchmarks scripts
if ($LASTEXITCODE -ne 0) { throw 'Ruff checks failed.' }
& .\.venv\Scripts\python.exe -m pre_commit run --all-files
if ($LASTEXITCODE -ne 0) { throw 'Pre-commit checks failed.' }

if (-not $env:RELAYCORE_TEST_DATABASE_URL) {
    docker compose up -d db
    if ($LASTEXITCODE -ne 0) { throw 'Start PostgreSQL with Docker or set RELAYCORE_TEST_DATABASE_URL.' }
    $env:RELAYCORE_TEST_DATABASE_URL = 'postgresql://relaycore:relaycore-local-change-me@127.0.0.1:5432/relaycore'
}
$env:DATABASE_URL = $env:RELAYCORE_TEST_DATABASE_URL
$env:RELAYCORE_DEMO_MODE = '1'
$env:RELAYCORE_WORKERS = '0'
$env:RELAYCORE_LEASE_SECONDS = '1.2'
$testTenant = 'relaycore-test-' + [guid]::NewGuid().ToString('N')
$otherTenant = 'relaycore-other-' + [guid]::NewGuid().ToString('N')
$env:RELAYCORE_API_KEYS = "{`"demo-key-change-me-32`":{`"tenant_id`":`"$testTenant`",`"role`":`"admin`"},`"viewer-key-change-me-32`":{`"tenant_id`":`"$testTenant`",`"role`":`"viewer`"},`"other-key-change-me-32`":{`"tenant_id`":`"$otherTenant`",`"role`":`"admin`"}}"
& .\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q
exit $LASTEXITCODE
