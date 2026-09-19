[CmdletBinding()]
param(
  # Removes the app without asking about the data. The macOS uninstaller has
  # always coped with having nobody to ask -- its prompt falls back to keeping
  # the data when there is no one at the keyboard -- and this one blocked
  # forever instead, which makes it impossible to script and impossible to test.
  [switch]$KeepData
)

$ErrorActionPreference = 'Stop'
$TaskName = 'SF Housing Monitor'
$AppRoot = Join-Path $env:LOCALAPPDATA 'SF Housing Monitor'
# Each in its own scope with Continue: Windows PowerShell 5.1 turns anything
# schtasks.exe says on stderr -- "the task does not exist", on a machine where
# it was never created -- into an error that would end the uninstall here.
& { $ErrorActionPreference = 'Continue'; & schtasks.exe /End /TN $TaskName 2>$null | Out-Null }
& { $ErrorActionPreference = 'Continue'; & schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null }

# Windows will not delete a file that is open, and the running app holds its
# own Python library open the whole time it is alive. schtasks /End asks it to
# stop; it does not wait for it to have stopped. Deleting immediately raced a
# process that was still shutting down and failed with "Access to the path
# ...\charset_normalizer\cd.cp312-win_amd64.pyd is denied", which leaves an
# install half removed and a person with no way forward.
function Stop-TheApp {
  foreach ($attempt in 1..40) {
    $alive = @(Get-Process -Name 'python' -ErrorAction SilentlyContinue | Where-Object {
      $_.Path -and $_.Path.StartsWith($AppRoot, [System.StringComparison]::OrdinalIgnoreCase)
    })
    if ($alive.Count -eq 0) { return }
    # Asked nicely for ten seconds; after that it is in the way of an uninstall
    # somebody has already confirmed.
    if ($attempt -eq 20) { $alive | Stop-Process -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
  }
}
Stop-TheApp

foreach ($name in @('current', 'python', 'uv', 'tools', 'releases')) {
  $path = Join-Path $AppRoot $name
  if (-not (Test-Path -LiteralPath $path)) { continue }
  # Even after the process is gone, antivirus and the indexer can hold a file
  # open for a moment. Worth a few retries before telling somebody it failed.
  foreach ($attempt in 1..10) {
    try { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop; break }
    catch {
      if ($attempt -eq 10) { throw "could not remove $path. Close anything using it, then run Uninstall again." }
      Start-Sleep -Milliseconds 500
    }
  }
}
Write-Host "SF Home Finder was removed. Your private data remains at:`n$AppRoot\data"
if ($KeepData) {
  $confirmation = ''
} else {
  # Nobody at the keyboard is an answer, and the answer is "keep it". Deleting
  # somebody's search history because a script had no stdin would be the worst
  # possible reading of silence.
  try { $confirmation = Read-Host 'Type DELETE to permanently remove private data, or press Enter to keep it' }
  catch { $confirmation = '' }
}
# -ceq, not -eq: PowerShell's -eq ignores case, so 'delete' -- the word a
# person types meaning "yes, uninstall" -- erased the database, the logs and
# every backup. The macOS script has always compared case-sensitively, and
# the README promises the data survives anything but an explicit DELETE.
if ($confirmation -ceq 'DELETE') {
  foreach ($name in @('data', 'logs', 'backups')) { $path = Join-Path $AppRoot $name; if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force } }
  Write-Host 'Private data was permanently deleted.'
} else { Write-Host 'Private data was preserved. A future install restores it.' }
