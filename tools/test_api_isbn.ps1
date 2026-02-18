# test_api_isbn.ps1
# Usage: .\tools\test_api_isbn.ps1 <isbn>

if ($args.Length -lt 1) {
  Write-Host "[ERROR] Please provide an ISBN"
  exit 1
}

if (-not $env:API_HOST) { $env:API_HOST = "127.0.0.1" }
if (-not $env:API_PORT) { $env:API_PORT = "8080" }
$hostUrl = $env:API_HOST
$port = $env:API_PORT
$base = "http://$hostUrl`:$port"
$isbn = $args[0]

Write-Host "[INFO] Testing $base/isbn/$isbn"
try {
  $resp = Invoke-WebRequest -Uri "$base/isbn/$isbn" -UseBasicParsing
  Write-Host $resp.Content
} catch {
  Write-Host "[ERROR] Failed: $($_.Exception.Message)"
  exit 1
}
