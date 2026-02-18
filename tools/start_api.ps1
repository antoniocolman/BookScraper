# start_api.ps1
# Usage: .\tools\start_api.ps1

if (-not $env:API_HOST) { $env:API_HOST = "127.0.0.1" }
if (-not $env:API_PORT) { $env:API_PORT = "8080" }

Write-Host "[INFO] Starting FastAPI on http://$($env:API_HOST):$($env:API_PORT)"
python -m app.api.main
