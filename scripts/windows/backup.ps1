$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..\.."))
New-Item -ItemType Directory -Force -Path backups | Out-Null

$dbName = ""
if (Test-Path .env) {
    foreach ($line in Get-Content .env) {
        if ($line -match '^\s*TURSO_DB_NAME=(.*)$') {
            $dbName = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}
if (-not $dbName) {
    Write-Error "Set TURSO_DB_NAME in .env (the database name, not the libsql URL)."
}
if (-not (Get-Command turso -ErrorAction SilentlyContinue)) {
    Write-Error "Install the Turso CLI from https://docs.turso.tech/cli/installation and run 'turso auth login'."
}

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$out = "backups\turso-$dbName-$stamp.sql"
turso db shell $dbName ".dump" | Out-File -Encoding utf8 $out
Write-Host "Wrote $out"
Write-Host "Also available without the CLI: .\.venv\Scripts\python.exe tools\export_backup.py"
Write-Host "Backup files contain encrypted private keys. Protect them."
