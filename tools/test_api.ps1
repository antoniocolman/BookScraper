# test_api.ps1
# Usage: .\tools\test_api.ps1

$hostUrl = $env:API_HOST -ne $null ? $env:API_HOST : "127.0.0.1"
$port = $env:API_PORT -ne $null ? $env:API_PORT : "8080"
$base = "http://$hostUrl`:$port"

Write-Host "[INFO] Testing $base/health"
try {
  $resp = Invoke-WebRequest -Uri "$base/health" -UseBasicParsing
  Write-Host $resp.Content
} catch {
  Write-Host "[ERROR] Failed: $($_.Exception.Message)"
  exit 1
}
