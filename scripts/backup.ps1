[CmdletBinding()]
param([Parameter(Mandatory, Position=0)][string]$Destination)

$ErrorActionPreference = 'Stop'
if (-not $env:DATABASE_URL) { throw 'Set DATABASE_URL to the PostgreSQL database to back up.' }
$pgDump = Get-Command pg_dump -ErrorAction Stop
$pgRestore = Get-Command pg_restore -ErrorAction Stop
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$repoPrefix = $repoRoot + [IO.Path]::DirectorySeparatorChar
$path = [IO.Path]::GetFullPath($Destination)
if ($path.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Store database backups outside the repository.'
}
$directory = [IO.Path]::GetDirectoryName($path)
if (-not (Test-Path -LiteralPath $directory -PathType Container)) { throw 'Backup destination directory does not exist.' }
if (Test-Path -LiteralPath $path) { throw 'Backup destination already exists; choose a new path.' }
$temporary = Join-Path $directory ('.' + [IO.Path]::GetFileName($path) + '.' + [guid]::NewGuid().ToString('N') + '.partial')

try {
    & $pgDump.Source '--format=custom' '--no-owner' '--no-privileges' "--dbname=$env:DATABASE_URL" "--file=$temporary"
    if ($LASTEXITCODE -ne 0) { throw 'pg_dump failed; the partial archive was removed.' }
    & $pgRestore.Source '--list' $temporary | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The generated backup archive did not pass pg_restore validation.' }
    [IO.File]::Move($temporary, $path)
    Write-Host "Backup created and archive-validated: $path"
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
}
