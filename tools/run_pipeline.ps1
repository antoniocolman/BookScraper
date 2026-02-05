param(
  [string]$ChunkDir   = ".\data\queries\isbn\chunks\lote_2026-01-20",
  [string]$Prefix     = "lote_2026-01-20",
  [string]$QueueBase  = ".\data\queues\lote_2026-01-20",
  [string]$ExportDir  = ".\data\exports\pipeline",
  [string]$LogDir     = ".\data\logs\pipeline",
  [string]$DbPath     = ".\data\runs\booksearchv2_new.db",

  [int]$Start = 1,
  [int]$End   = 161,

  [switch]$Resume,
  [switch]$SkipYenny,
  [switch]$AutoSkipYennyOn429,

  # Delays por sitio (ajustables)
  [double]$DelayElLector     = 0.8,
  [double]$QueryDelayElLector= 0.2,

  [double]$DelayYenny        = 1.0,
  [double]$QueryDelayYenny   = 6.0,

  [double]$DelayContrapunto  = 1.0,
  [double]$QueryDelayContrapunto = 0.4,

  [double]$DelayBookpeople   = 1.0,
  [double]$QueryDelayBookpeople= 0.4
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $QueueBase | Out-Null
New-Item -ItemType Directory -Force -Path $ExportDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DbPath) | Out-Null

$globalRemaining = Join-Path $QueueBase "remaining_all.txt"
$globalRetryYenny = Join-Path $QueueBase "retry_yenny.txt"

function New-MissingQueue {
  param(
    [string]$InputFile,
    [string]$CsvFile,
    [string]$OutFile
  )

  $in = Get-Content -LiteralPath $InputFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" -and -not $_.StartsWith("#") }

  if (!(Test-Path -LiteralPath $CsvFile)) {
    $in | Set-Content -Encoding UTF8 -LiteralPath $OutFile
    Write-Host "[queue] CSV no existe. missing = input ($($in.Count)) -> $OutFile"
    return
  }

  $found = @{}
  try {
    Import-Csv -LiteralPath $CsvFile | ForEach-Object {
      $isbn = $_.ISBN
      if (-not $isbn) { $isbn = $_.isbn }
      if ($isbn) {
        $n = ($isbn.ToString() -replace '[^0-9Xx]', '').ToUpper()
        if ($n) { $found[$n] = $true }
      }
    }
  } catch {
    # si el csv está vacío o malformado, tratamos como "no encontró nada"
    $in | Set-Content -Encoding UTF8 -LiteralPath $OutFile
    Write-Host "[queue] CSV ilegible. missing = input ($($in.Count)) -> $OutFile"
    return
  }

  $missing = foreach ($q in $in) {
    $nq = ($q -replace '[^0-9Xx]', '').ToUpper()
    if (-not $nq) { $q }      # si no parece ISBN, lo dejamos pasar
    elseif (-not $found.ContainsKey($nq)) { $q }
  }

  $missing | Set-Content -Encoding UTF8 -LiteralPath $OutFile
  $mCount = ($missing | Measure-Object).Count
  Write-Host "[queue] input=$($in.Count) found=$($found.Count) missing=$mCount -> $OutFile"
}

function Invoke-AppRun {
  param(
    [string]$Site,
    [string]$QueryFile,
    [string]$CsvOut,
    [double]$Delay,
    [double]$QueryDelay,
    [string]$LogFile
  )

  $args = @(
    "-m","app","run",
    "--site",$Site,
    "--query-file",$QueryFile,
    "--write-db",
    "--db-path",$DbPath,
    "--output",$CsvOut,
    "--delay",$Delay.ToString(),
    "--query-delay",$QueryDelay.ToString()
  )

  # Ejecuta y guarda log (stdout+stderr)
  $out = & python @args 2>&1
  $out | Tee-Object -FilePath $LogFile -Append

  $rc = $LASTEXITCODE
  $text = ($out -join "`n")
  return [pscustomobject]@{
    ExitCode = $rc
    Output   = $text
  }
}

for ($i = $Start; $i -le $End; $i++) {
  $chunkId = "{0:0000}" -f $i
  $chunkFile = Join-Path $ChunkDir ("{0}_{1}.txt" -f $Prefix, $chunkId)
  if (!(Test-Path -LiteralPath $chunkFile)) {
    Write-Host "[skip] No existe chunk: $chunkFile"
    continue
  }

  $qdir = Join-Path $QueueBase $chunkId
  New-Item -ItemType Directory -Force -Path $qdir | Out-Null

  $inFile = Join-Path $qdir "in.txt"
  Copy-Item -Force $chunkFile $inFile

  $remainingFile = Join-Path $qdir "remaining.txt"
  if ($Resume -and (Test-Path -LiteralPath $remainingFile)) {
    Write-Host "[resume] chunk $chunkId ya tiene remaining.txt, salto."
    continue
  }

  $logFile = Join-Path $LogDir ("pipeline_{0}.log" -f $chunkId)
  "`n==== CHUNK $chunkId ====`n" | Out-File -FilePath $logFile -Encoding UTF8 -Append

  Write-Host "`n===== CHUNK $chunkId ====="

  # 1) EL LECTOR
  $csvEl = Join-Path $ExportDir ("el_lector_{0}.csv" -f $chunkId)
  $rEl = Invoke-AppRun -Site "el_lector" -QueryFile $inFile -CsvOut $csvEl -Delay $DelayElLector -QueryDelay $QueryDelayElLector -LogFile $logFile
  if ($rEl.ExitCode -ne 0) { Write-Host "[warn] el_lector rc=$($rEl.ExitCode)"; }

  $qYenny = Join-Path $qdir "q_yenny.txt"
  New-MissingQueue $inFile $csvEl $qYenny

  # 2) YENNY (opcional / autoskip 429)
  $csvY = Join-Path $ExportDir ("yenny_search_{0}.csv" -f $chunkId)
  $qContra = Join-Path $qdir "q_contrapunto.txt"

  if ($SkipYenny) {
    Copy-Item -Force $qYenny $qContra
    Write-Host "[skip] yenny_search deshabilitado (SkipYenny)."
  } else {
    $rY = Invoke-AppRun -Site "yenny_search" -QueryFile $qYenny -CsvOut $csvY -Delay $DelayYenny -QueryDelay $QueryDelayYenny -LogFile $logFile

    $is429 = ($rY.Output -match "429") -or ($rY.Output -match "Too Many Requests")
    if ($AutoSkipYennyOn429 -and $is429) {
      Write-Host "[warn] yenny_search 429 detectado. Encolando retry_yenny y saltando."
      Add-Content -Encoding UTF8 -LiteralPath $globalRetryYenny -Value (Get-Content -LiteralPath $qYenny)
      Copy-Item -Force $qYenny $qContra
    } else {
      if ($rY.ExitCode -ne 0) { Write-Host "[warn] yenny_search rc=$($rY.ExitCode)"; }
      New-MissingQueue $qYenny $csvY $qContra
    }
  }

  # 3) CONTRAPUNTO
  $csvC = Join-Path $ExportDir ("contrapunto_{0}.csv" -f $chunkId)
  $rC = Invoke-AppRun -Site "contrapunto" -QueryFile $qContra -CsvOut $csvC -Delay $DelayContrapunto -QueryDelay $QueryDelayContrapunto -LogFile $logFile
  if ($rC.ExitCode -ne 0) { Write-Host "[warn] contrapunto rc=$($rC.ExitCode)"; }

  $qBP = Join-Path $qdir "q_bookpeople.txt"
  New-MissingQueue $qContra $csvC $qBP

  # 4) BOOKPEOPLE
  $csvBP = Join-Path $ExportDir ("bookpeople_{0}.csv" -f $chunkId)
  $rBP = Invoke-AppRun -Site "bookpeople" -QueryFile $qBP -CsvOut $csvBP -Delay $DelayBookpeople -QueryDelay $QueryDelayBookpeople -LogFile $logFile
  if ($rBP.ExitCode -ne 0) { Write-Host "[warn] bookpeople rc=$($rBP.ExitCode)"; }

  New-MissingQueue $qBP $csvBP $remainingFile

  # Acumular remaining global
  if (Test-Path -LiteralPath $remainingFile) {
    Add-Content -Encoding UTF8 -LiteralPath $globalRemaining -Value (Get-Content -LiteralPath $remainingFile)
  }

  Write-Host "[done] chunk $chunkId -> $remainingFile"
}

Write-Host "`n[OK] Pipeline finalizado."
Write-Host "Remaining global: $globalRemaining"
Write-Host "Retry Yenny:      $globalRetryYenny"