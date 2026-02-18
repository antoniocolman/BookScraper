# test_api_missing.ps1
# Usage: .\tools\test_api_missing.ps1 [limit]

$hostUrl = $env:API_HOST -ne $null ? $env:API_HOST : "127.0.0.1"
$port = $env:API_PORT -ne $null ? $env:API_PORT : "8080"
$base = "http://$hostUrl`:$port"
$limit = if ($args.Length -gt 0) { $args[0] } else { 1 }

Write-Host "[INFO] Testing $base/missing?limit=$limit"
try {
  $resp = Invoke-WebRequest -Uri "$base/missing?limit=$limit" -UseBasicParsing
  Write-Host $resp.Content
} catch {
  Write-Host "[ERROR] Failed: $($_.Exception.Message)"
  exit 1
}
