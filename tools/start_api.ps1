# start_api.ps1
# Usage: .\tools\start_api.ps1

$env:API_HOST = $env:API_HOST -ne $null ? $env:API_HOST : "127.0.0.1"
$env:API_PORT = $env:API_PORT -ne $null ? $env:API_PORT : "8080"

Write-Host "[INFO] Starting FastAPI on http://$($env:API_HOST):$($env:API_PORT)"
python -m app.api.main
