param(
    [Parameter(Mandatory = $true)]
    [string]$Dump
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..\.."))

if (-not (Test-Path $Dump)) {
    Write-Error "Dump file not found: $Dump"
}

$dbName = ""
if (Test-Path .env) {
    foreach ($line in Get-Content .env) {
        if ($line -match '^\s*TURSO_DB_NAME=(.*)$') {
            $dbName = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}
if (-not $dbName) { Write-Error "Set TURSO_DB_NAME in .env." }
if (-not (Get-Command turso -ErrorAction SilentlyContinue)) {
    Write-Error "Install the Turso CLI and run 'turso auth login'."
}

Write-Host "This replays SQL into '$dbName'."
Write-Host "Use an empty database if this one already has tables: turso db create <new-name>"
$confirm = Read-Host "Type the database name to continue"
if ($confirm -ne $dbName) {
    Write-Host "Aborted."
    exit 1
}
Get-Content $Dump | turso db shell $dbName
Write-Host "Restore command finished. Run: .\.venv\Scripts\python.exe migrate.py"
Write-Host "MASTER_ENCRYPTION_KEY must be the same key that encrypted the wallets."
