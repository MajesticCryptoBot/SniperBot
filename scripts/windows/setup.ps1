$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..\.."))

$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = "py"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
} else {
    Write-Error "Install Python 3.11+ from https://www.python.org/downloads/ and enable 'Add python.exe to PATH'."
}

if ($python -eq "py") {
    & py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) { & py -3 -m venv .venv }
} else {
    & python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env. Fill it in before migrating."
}
New-Item -ItemType Directory -Force -Path logs, backups | Out-Null
Write-Host "Next: edit .env"
Write-Host "Then: .\.venv\Scripts\python.exe migrate.py"
Write-Host "Then: .\.venv\Scripts\python.exe bot.py"
Write-Host "Stop the bot with Ctrl+C in that window."
