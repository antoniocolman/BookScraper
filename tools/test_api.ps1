# test_api.ps1
# Usage: .\tools\test_api.ps1

if (-not $env:API_HOST) { $env:API_HOST = "127.0.0.1" }
if (-not $env:API_PORT) { $env:API_PORT = "8080" }
$hostUrl = $env:API_HOST
$port = $env:API_PORT
$base = "http://$hostUrl`:$port"

Write-Host "[INFO] Testing $base/health"
try {
  $resp = Invoke-WebRequest -Uri "$base/health" -UseBasicParsing
  Write-Host $resp.Content
} catch {
  Write-Host "[ERROR] Failed: $($_.Exception.Message)"
  exit 1
}
