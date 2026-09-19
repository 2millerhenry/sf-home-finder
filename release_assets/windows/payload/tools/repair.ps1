[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ReleaseRoot)

$ErrorActionPreference = 'Stop'
$TaskName = 'SF Housing Monitor'
# Where install.ps1 and uninstall.ps1 put it, found the same way, so Repair
# restores the data the installed app actually uses.
$AppRoot = Join-Path $env:LOCALAPPDATA 'SF Housing Monitor'
$installer = [IO.Path]::Combine($ReleaseRoot, 'payload', 'install.ps1')
if (-not (Test-Path -LiteralPath $installer)) { throw 'Repair needs the extracted SF Home Finder folder. Extract the ZIP and double-click Repair there.' }

# Runs a program for its exit code alone. Windows PowerShell 5.1 -- what a
# double-clicked .cmd runs -- turns anything a program writes to stderr into a
# terminating error when $ErrorActionPreference is Stop and stderr is
# redirected, so a schtasks.exe saying "the task is not running" would end
# Repair before it had looked at the data. Scoped to this function; everything
# else in the script still stops on the first error.
function Invoke-Quietly([string]$Program, [string[]]$Arguments) {
  $ErrorActionPreference = 'Continue'
  & $Program @Arguments 2>$null | Out-Null
  return $LASTEXITCODE
}

# This install's own Python processes -- never another app's.
function Get-AppProcess {
  @(Get-Process -Name 'python' -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path.StartsWith($AppRoot, [System.StringComparison]::OrdinalIgnoreCase)
  })
}

# True once nothing of the app's is left running. /End asks the task to stop
# rather than waiting for it to have stopped, and a restore under a process
# still writing could lose what it writes. The same wait install.ps1 makes;
# checked even with no task to end, since the app can be started other ways.
function Stop-App {
  if (Get-Command schtasks.exe -ErrorAction SilentlyContinue) {
    $null = Invoke-Quietly 'schtasks.exe' @('/End', '/TN', $TaskName)
  }
  foreach ($attempt in 1..40) {
    $alive = Get-AppProcess
    if ($alive.Count -eq 0) { return $true }
    if ($attempt -eq 20) { $alive | Stop-Process -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
  }
  return ((Get-AppProcess).Count -eq 0)
}

function Start-App {
  if (-not (Get-Command schtasks.exe -ErrorAction SilentlyContinue)) { return }
  $null = Invoke-Quietly 'schtasks.exe' @('/Run', '/TN', $TaskName)
}

# The data comes first, and it does not wait on the reinstall: somebody running
# Repair may have no internet, and getting their homes back must not depend on
# downloading a runtime. This used to be the whole of Windows Repair -- run the
# installer -- so a database that could not be read was never restored here,
# while the app's own error told people Repair would restore it.
#
# The rules are the ones in this release's own wheel, run by a Python this
# machine already has: the installed app may be the thing that is broken, and
# repairing an older install must apply these rules rather than the ones it
# shipped with. They live in sf_housing/backups.py -- the same code the macOS
# Repair and both installers run -- and are tested there.
$database = [IO.Path]::Combine($AppRoot, 'data', 'housing.sqlite3')
if (Test-Path -LiteralPath $database) {
  $candidates = @([IO.Path]::Combine($AppRoot, 'current', 'Scripts', 'python.exe'))
  $managed = [IO.Path]::Combine($AppRoot, 'python')
  if (Test-Path -LiteralPath $managed) {
    $candidates += @(Get-ChildItem -LiteralPath $managed -Filter 'python.exe' -Recurse -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
  }
  $python = $null
  foreach ($candidate in $candidates) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    if ((Invoke-Quietly $candidate @('-I', '-c', 'import sqlite3')) -eq 0) { $python = $candidate; break }
  }
  $wheel = Get-ChildItem -LiteralPath ([IO.Path]::Combine($ReleaseRoot, 'payload')) -Filter 'sf_home_finder-*.whl' -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName

  if (-not $python -or -not $wheel) {
    # Failing toward the data: without a way to run the rules, the database is
    # left exactly as it is rather than touched by something cruder.
    Write-Host 'Could not check your housing data on this machine, so it was left exactly as it is.'
  } elseif (-not (Stop-App)) {
    Write-Host 'SF Home Finder would not stop, so your housing data was left exactly as it is.'
  } else {
    # Isolated (-I), so nothing in the environment or the current folder can
    # stand in for the release's package, and the wheel is put first on the
    # path by hand. No double quotes anywhere in the program text: Windows
    # PowerShell 5.1 mangles them on the way to a native command.
    & $python -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from sf_housing.backups import main; sys.exit(main(sys.argv[2:]))' $wheel repair $AppRoot
    $status = $LASTEXITCODE
    if ($status -eq 2) {
      # No safety copy could be taken, so nothing else may go ahead: not a
      # restore, not a reinstall that would rewrite the app around the data.
      Start-App
      exit 2
    }
    # 3 means the database could not be read and there was nothing good to
    # restore. Nothing was deleted; the reinstall below still fixes the app.
    if ($status -ne 0 -and $status -ne 3) {
      # Anything else is the rules themselves failing, and silence would read
      # as "your data is fine".
      Write-Host 'Checking your housing data stopped with an error (shown above); nothing more was done to it.'
    }
  }
}

Write-Host 'Repairing application files. Your profile, listings, and connectors will be preserved.'
# The PowerShell already running this, rather than powershell.exe by name: the
# same program when a .cmd started it, and runnable wherever this script is.
$shell = (Get-Process -Id $PID).Path
& $shell -NoLogo -NoProfile -ExecutionPolicy Bypass -File $installer -ReleaseRoot $ReleaseRoot
$installed = $LASTEXITCODE
if ($installed -ne 0) {
  # Repair stopped the app above; a reinstall that failed before starting it
  # again must not leave it off until the next sign-in.
  Start-App
}
exit $installed
