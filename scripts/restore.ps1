[CmdletBinding(SupportsShouldProcess, ConfirmImpact='High')]
param(
    [Parameter(Mandatory)][string]$BackupPath,
    [Parameter(Mandatory)][string]$TargetDatabaseUrl
)

$ErrorActionPreference = 'Stop'
if ($TargetDatabaseUrl -notmatch '^postgres(?:ql)?://[^/]+/[^/?]+') {
    throw 'TargetDatabaseUrl must name an explicit PostgreSQL database in its connection URL.'
}
$pgRestore = Get-Command pg_restore -ErrorAction Stop
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$repoPrefix = $repoRoot + [IO.Path]::DirectorySeparatorChar
$path = [IO.Path]::GetFullPath($BackupPath)
if ($path.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Keep database backups outside the repository.'
}
if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Backup archive does not exist.' }
& $pgRestore.Source '--list' $path | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Backup archive failed validation; no restore was attempted.' }

if ($PSCmdlet.ShouldProcess('the selected PostgreSQL database', 'restore this archive')) {
    # No --clean: a non-empty target must fail and roll back rather than deleting existing objects.
    & $pgRestore.Source '--single-transaction' '--exit-on-error' '--no-owner' '--no-privileges' `
        "--dbname=$TargetDatabaseUrl" $path
    if ($LASTEXITCODE -ne 0) { throw 'Restore failed; pg_restore rolled back the transaction.' }
    Write-Host 'Backup restored successfully.'
}
