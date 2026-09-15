[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$ReleaseRoot
)

$ErrorActionPreference = 'Stop'
$Version = '0.5.4'
$PythonVersion = '3.12.10'
$Port = 8000
$TaskName = 'SF Housing Monitor'
$AppRoot = Join-Path $env:LOCALAPPDATA 'SF Housing Monitor'
$Payload = Join-Path $ReleaseRoot 'payload'
$DataDir = Join-Path $AppRoot 'data'
$LogDir = Join-Path $AppRoot 'logs'
$ToolsDir = Join-Path $AppRoot 'tools'
$RuntimeTarget = Join-Path $AppRoot 'current'
$UvVersion = '0.10.8'
$UvUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$UvSha256 = '2E70ECD22196CBD9D14EEFB700814BCAFC5B75A0D8275B52E8402E5FE256D928'
$UvDir = Join-Path $AppRoot "uv\$UvVersion"
$UvExe = Join-Path $UvDir 'uv.exe'
$LockFile = Join-Path $Payload 'requirements.lock'
$WheelFile = Join-Path $Payload "sf_home_finder-$Version-py3-none-any.whl"

function Fail([string]$Message) {
  throw "Installation stopped: $Message"
}

function Test-HealthyMonitor {
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
    return $health.app -eq 'sf-home-finder' -and $health.ok -eq $true
  } catch {
    return $false
  }
}

# 45 seconds was not enough, which the macOS installer learned in 0.4.2 and
# this never did. A first start on a full board spends about fifteen seconds
# re-ranking what is already stored, and a slower disk or a larger board spends
# more, so a normal install could be told it had failed seconds before it
# answered -- and be sent to Repair for a problem it did not have.
function Wait-ForMonitor([int]$Seconds = 150) {
  foreach ($attempt in 1..$Seconds) {
    if (Test-HealthyMonitor) { return $true }
    Start-Sleep -Seconds 1
  }
  return $false
}

function Test-PortInUse {
  try {
    return $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
  } catch {
    return $false
  }
}

function Invoke-Uv([string[]]$Arguments) {
  & $UvExe @Arguments
  if ($LASTEXITCODE -ne 0) { Fail "the private runtime setup failed. Check your internet connection, then run Repair." }
}

if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { Fail 'this build supports x64 Windows only. Ask for an ARM64 build instead of forcing this one.' }
if (-not (Test-Path -LiteralPath $Payload)) { Fail 'the release folder is incomplete. Extract the ZIP again, then run Install.' }
foreach ($required in @($LockFile, $WheelFile, (Join-Path $Payload 'checksums.sha256'))) {
  if (-not (Test-Path -LiteralPath $required)) { Fail "the release is incomplete ($([IO.Path]::GetFileName($required)) is missing). Extract the ZIP again." }
}

# Windows stamps every file extracted from a downloaded ZIP with a
# Mark-of-the-Web alternate data stream, which is what makes SmartScreen and
# PowerShell challenge each script in turn. The user has already chosen to run
# this installer, so clear the mark once for the whole release. It changes no
# file content, so the checksum verification below is unaffected.
Get-ChildItem -LiteralPath $ReleaseRoot -Recurse -File -ErrorAction SilentlyContinue |
  Unblock-File -ErrorAction SilentlyContinue

$checksumLines = Get-Content -LiteralPath (Join-Path $Payload 'checksums.sha256')
foreach ($line in $checksumLines) {
  if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { Fail 'the release checksum file is invalid. Extract the ZIP again.' }
  $expected = $Matches[1].ToUpperInvariant()
  $path = Join-Path $Payload $Matches[2]
  if (-not (Test-Path -LiteralPath $path)) { Fail "release verification failed ($($Matches[2]) is missing). Extract the ZIP again." }
  if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToUpperInvariant() -ne $expected) { Fail "release verification failed ($($Matches[2]) changed). Extract the ZIP again." }
}

if ((Test-PortInUse) -and -not (Test-HealthyMonitor)) { Fail "port $Port is already used by another app. Close that app, then run Install again. Nothing was stopped." }

Write-Host "Installing SF Home Finder $Version for Windows..."
New-Item -ItemType Directory -Force -Path $DataDir, $LogDir, $ToolsDir, (Join-Path $AppRoot 'releases'), $UvDir | Out-Null

$stage = Join-Path $AppRoot ('.install.' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
try {
  if (-not (Test-Path -LiteralPath $UvExe)) {
    Write-Host 'Downloading the verified private runtime bootstrap...'
    $uvZip = Join-Path $stage 'uv.zip'
    Invoke-WebRequest -Uri $UvUrl -OutFile $uvZip -UseBasicParsing
    if ((Get-FileHash -LiteralPath $uvZip -Algorithm SHA256).Hash.ToUpperInvariant() -ne $UvSha256) { Fail 'the runtime bootstrap checksum did not match. Nothing was installed.' }
    $uvExtract = Join-Path $stage 'uv'
    Expand-Archive -LiteralPath $uvZip -DestinationPath $uvExtract -Force
    $downloadedUv = Get-ChildItem -LiteralPath $uvExtract -Recurse -Filter 'uv.exe' | Select-Object -First 1
    if ($null -eq $downloadedUv) { Fail 'the verified runtime bootstrap did not contain uv.exe.' }
    Copy-Item -LiteralPath $downloadedUv.FullName -Destination $UvExe -Force
  }

  if (Test-HealthyMonitor) {
    & schtasks.exe /End /TN $TaskName 2>$null | Out-Null
    # Waited two seconds and hoped. schtasks /End asks a process to stop
    # rather than waiting for it to have stopped, and Windows will not delete
    # a file that is open -- so an upgrade begun while the app was still
    # shutting down failed on its own Python library further down, where the
    # old runtime is removed. Two seconds is usually enough, which is the worst
    # kind of usually: it fails on the slow machines and the busy ones, which
    # are exactly the ones an upgrade takes longest on.
    foreach ($attempt in 1..40) {
      $alive = @(Get-Process -Name 'python' -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($AppRoot, [System.StringComparison]::OrdinalIgnoreCase)
      })
      if ($alive.Count -eq 0) { break }
      if ($attempt -eq 20) { $alive | Stop-Process -Force -ErrorAction SilentlyContinue }
      Start-Sleep -Milliseconds 500
    }
  }

  Write-Host 'Preparing the private Python runtime...'
  Invoke-Uv -Arguments @('python', 'install', $PythonVersion, '--install-dir', (Join-Path $AppRoot 'python'), '--no-bin', '--no-progress')
  $stageRuntime = Join-Path $stage 'runtime'
  Invoke-Uv -Arguments @('venv', $stageRuntime, '--python', $PythonVersion, '--managed-python', '--no-project')
  $stagePython = Join-Path $stageRuntime 'Scripts\python.exe'
  Invoke-Uv -Arguments @('pip', 'sync', $LockFile, '--python', $stagePython, '--strict', '--no-progress')
  Invoke-Uv -Arguments @('pip', 'install', $WheelFile, '--python', $stagePython, '--no-deps', '--no-progress')
  & $stagePython -I -c "import sf_housing; assert sf_housing.__version__ == '$Version'"
  if ($LASTEXITCODE -ne 0) { Fail 'the installed app did not pass its version check.' }

  if ((Test-Path -LiteralPath (Join-Path $DataDir 'housing.sqlite3')) -and (Test-Path -LiteralPath (Join-Path $RuntimeTarget 'Scripts\python.exe'))) {
    $backupDir = Join-Path $AppRoot 'backups'
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    & (Join-Path $RuntimeTarget 'Scripts\python.exe') -c "import sqlite3,sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()" (Join-Path $DataDir 'housing.sqlite3') (Join-Path $backupDir "housing-$stamp.sqlite3")
  }

  if (Test-Path -LiteralPath $RuntimeTarget) {
    # And retried, because antivirus and the search indexer can hold a file
    # open for a moment after the process that owned it is gone.
    foreach ($attempt in 1..10) {
      try { Remove-Item -LiteralPath $RuntimeTarget -Recurse -Force -ErrorAction Stop; break }
      catch {
        if ($attempt -eq 10) { Fail 'the previous version could not be replaced. Close anything using SF Home Finder, then run Install again.' }
        Start-Sleep -Milliseconds 500
      }
    }
  }
  Move-Item -LiteralPath $stageRuntime -Destination $RuntimeTarget
  Copy-Item -LiteralPath (Join-Path $Payload 'tools\run-service.cmd') -Destination (Join-Path $ToolsDir 'run-service.cmd') -Force
  Get-ChildItem -LiteralPath (Join-Path $Payload 'tools') -Filter '*.ps1' | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $ToolsDir -Force }
  $releaseDir = Join-Path $AppRoot "releases\$Version"
  New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
  Copy-Item -LiteralPath $WheelFile, $LockFile -Destination $releaseDir -Force
  $bridgeSource = Join-Path $Payload 'furnished-finder-bridge'
  $bridgeTarget = Join-Path $AppRoot 'furnished-finder-bridge'
  if (Test-Path -LiteralPath $bridgeSource) {
    if (Test-Path -LiteralPath $bridgeTarget) { Remove-Item -LiteralPath $bridgeTarget -Recurse -Force }
    Copy-Item -LiteralPath $bridgeSource -Destination $bridgeTarget -Recurse -Force
  }
  if ((Test-Path -LiteralPath (Join-Path $Payload 'gmail-client-secret.json')) -and -not (Test-Path -LiteralPath (Join-Path $DataDir 'gmail-client-secret.json'))) { Copy-Item -LiteralPath (Join-Path $Payload 'gmail-client-secret.json') -Destination (Join-Path $DataDir 'gmail-client-secret.json') }

  $serviceCommand = '"' + (Join-Path $ToolsDir 'run-service.cmd') + '"'
  & schtasks.exe /Create /TN $TaskName /TR $serviceCommand /SC ONLOGON /RL LIMITED /F | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail 'Windows could not create the normal-user startup task. No administrator account is required, but this Windows account must be allowed to create scheduled tasks.' }
  & schtasks.exe /Run /TN $TaskName | Out-Null
  if ($LASTEXITCODE -ne 0 -or -not (Wait-ForMonitor)) { Fail "the local dashboard did not become healthy. Run Repair; details are in $LogDir." }
  Write-Host "Installed. Your profile and history stay in: $DataDir"
  # Opening a browser is a courtesy, not part of installing. A machine with no
  # browser association, or one being installed without a desktop session,
  # would otherwise throw here and report a failure for an install that
  # succeeded -- everything above this line has already worked.
  if ($env:SF_HOUSING_NO_BROWSER -ne '1') {
    try { Start-Process "http://127.0.0.1:$Port/" } catch {
      Write-Host "Open it yourself at http://127.0.0.1:$Port/"
    }
  }
} finally {
  if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
}
