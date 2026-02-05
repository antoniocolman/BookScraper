param(
  [string]$Lote = "lote_2026-01-20",
  [string]$Db = ".\data\booksearchv2.db",
  [string]$SiteParams = ".\data\site_params.json",
  [string[]]$Sites = @("el_lector","bookpeople","contrapunto","cuspide","casa_del_libro","yenny_search"),

  # Si está seteado, arranca desde ese chunk (ej: "0010")
  [string]$StartChunk = "",

  # Si está presente, vuelve a copiar in.txt aunque ya exista en la carpeta de cola
  [switch]$ResetQueues,

  # Si está presente, saltea chunks que ya tienen final_XXXX.csv
  [switch]$SkipIfDone
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Normalize-Isbn([string]$s) {
  if ($null -eq $s) { return "" }
  $n = ($s -replace "[^0-9Xx]", "").ToUpper().Trim()
  return $n
}

function Update-RemainingAll([string]$RemainingTxt, [string]$RemainingAll) {
  if (!(Test-Path $RemainingTxt)) { return }

  # Append
  Get-Content $RemainingTxt | Add-Content -Encoding utf8 $RemainingAll

  # Normalize + dedupe + keep only "isbn-ish" lines (10/13 digits)
  $norm = Get-Content $RemainingAll |
    ForEach-Object { Normalize-Isbn $_ } |
    Where-Object { $_ -ne "" -and ($_ -match "^\d{10}(\d{3})?$") } |
    Sort-Object -Unique

  # Rewrite
  $norm | Set-Content -Encoding utf8 $RemainingAll
}

function Write-AmazonQueueCsv([string]$RemainingAll, [string]$AmazonCsvOut) {
  if (!(Test-Path $RemainingAll)) { return }

  $outDir = Split-Path -Parent $AmazonCsvOut
  if ($outDir -and !(Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }

  "isbn,site,url,titulo,amazon_search_url" | Set-Content -Encoding utf8 $AmazonCsvOut

  Get-Content $RemainingAll | ForEach-Object {
    $isbn = Normalize-Isbn $_
    if ($isbn -and ($isbn -match "^\d{10}(\d{3})?$")) {
      $line = "$isbn,,,,https://www.amazon.com/s?k=$isbn&i=stripbooks"
      Add-Content -Encoding utf8 $AmazonCsvOut $line
    }
  }
}

$chunksDir = ".\data\queries\isbn\chunks\$Lote"
$outDir = ".\data\exports\pipeline"
$queueRoot = ".\data\queues\$Lote"

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
New-Item -ItemType Directory -Force -Path $queueRoot | Out-Null

# Cola acumulada + CSV para Amazon (siempre actualizados)
$remainingAll = Join-Path $queueRoot "remaining_all.txt"
$amazonCsv = ".\data\exports\enrich_queue_amazon.csv"

# Ensure files exist
if (!(Test-Path $remainingAll)) { "" | Set-Content -Encoding utf8 $remainingAll }

# Normaliza StartChunk a número (ej "0010" -> 10). Si está vacío, no filtra.
$startN = $null
if ($StartChunk -and $StartChunk.Trim() -ne "") {
  try { $startN = [int]$StartChunk } catch { $startN = $null }
}

# Quick sanity checks
if (!(Test-Path $chunksDir)) { throw "No existe chunksDir: $chunksDir" }
if (!(Test-Path $Db)) { throw "No existe DB: $Db" }
if ($SiteParams -and !(Test-Path $SiteParams)) { Write-Host "[WARN] site_params no existe: $SiteParams (se usará default del CLI)" }

Get-ChildItem "$chunksDir\${Lote}_*.txt" |
  Sort-Object Name |
  Where-Object {
    if ($null -eq $startN) { return $true }
    $cid = ($_.BaseName -replace "^${Lote}_","")
    try { return ([int]$cid -ge $startN) } catch { return $false }
  } |
  ForEach-Object {

  $file = $_.FullName
  $chunkId = ($_.BaseName -replace "^${Lote}_","")

  $qdir = Join-Path $queueRoot $chunkId
  New-Item -ItemType Directory -Force -Path $qdir | Out-Null

  $inPath = Join-Path $qdir "in.txt"
  if ($ResetQueues -or !(Test-Path $inPath)) {
    Copy-Item -Force $file $inPath
  }

  $finalCsv = Join-Path $outDir ("final_{0}.csv" -f $chunkId)
  $remainingTxt = Join-Path $qdir "remaining.txt"

  if ($SkipIfDone -and (Test-Path $finalCsv)) {
    Write-Host "[SKIP] $chunkId -> ya existe $finalCsv"
    return
  }

  Write-Host "`n=== CHUNK $chunkId ==="
  python -m app sync `
    --query-file $inPath `
    --sites $Sites `
    --site-params-file $SiteParams `
    --db-path $Db `
    --output $finalCsv `
    --remaining-out $remainingTxt

  if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] chunk $chunkId falló (exit=$LASTEXITCODE). Cortando."
    exit $LASTEXITCODE
  }

  # --- Post-chunk: acumular remaining y regenerar CSV Amazon ---
  Update-RemainingAll $remainingTxt $remainingAll
  Write-AmazonQueueCsv $remainingAll $amazonCsv

  $cnt = 0
  try { $cnt = (Get-Content $remainingAll | Measure-Object -Line).Lines } catch { $cnt = 0 }
  Write-Host "[QUEUE] remaining_all=$cnt -> $remainingAll"
  Write-Host "[QUEUE] amazon_csv -> $amazonCsv"
}

Write-Host "`n[OK] Lote finalizado. remaining_all -> $remainingAll | amazon_csv -> $amazonCsv"
