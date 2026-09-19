$ErrorActionPreference = 'Stop'
$Port = 8000
$TaskName = 'SF Housing Monitor'
$AppRoot = Join-Path $env:LOCALAPPDATA 'SF Housing Monitor'
$Url = "http://127.0.0.1:$Port/"

function Test-HealthyMonitor {
  try { $health = Invoke-RestMethod -Uri ($Url + 'health') -TimeoutSec 2; return $health.app -eq 'sf-home-finder' -and $health.ok -eq $true } catch { return $false }
}
if (-not (Test-HealthyMonitor)) {
  # Its own scope with Continue: Windows PowerShell 5.1 turns a schtasks.exe
  # complaint on stderr into an error under Stop, before the wait below.
  & { $ErrorActionPreference = 'Continue'; & schtasks.exe /Run /TN $TaskName 2>$null | Out-Null }
  foreach ($attempt in 1..30) { if (Test-HealthyMonitor) { break }; Start-Sleep -Seconds 1 }
}
if (-not (Test-HealthyMonitor)) { throw "The dashboard did not start. Double-click Repair SF Home Finder. Logs are in $AppRoot\logs." }
Start-Process $Url
Write-Host "SF Home Finder is ready at $Url"
