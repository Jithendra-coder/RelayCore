$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host 'Created .env with the loopback-only Demo Mode key.'
}

try {
    docker info | Out-Null
} catch {
    throw 'Docker Desktop is not running. Start Docker Desktop, then rerun .\scripts\demo.ps1.'
}
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Desktop is not running. Start Docker Desktop, then rerun .\scripts\demo.ps1.'
}

if ($env:RELAYCORE_PORT) {
    $port = [int]$env:RELAYCORE_PORT
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. Set RELAYCORE_PORT to a free loopback port."
    }
} else {
    $port = 8000
    while ($port -le 8010 -and (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
        $port++
    }
    if ($port -gt 8010) { throw 'Ports 8000–8010 are all in use. Set RELAYCORE_PORT to a free port.' }
    $env:RELAYCORE_PORT = "$port"
}

docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose could not start RelayCore.' }

$ready = $false
for ($i = 0; $i -lt 45; $i++) {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 2
        if ($response.status -eq 'ok') { $ready = $true; break }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) {
    docker compose logs --tail 80 api db
    throw 'RelayCore did not become healthy. See the container logs above.'
}

Write-Host "RelayCore is ready at http://127.0.0.1:$port"
Start-Process "http://127.0.0.1:$port"
