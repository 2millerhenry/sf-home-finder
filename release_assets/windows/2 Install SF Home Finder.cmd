@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0payload\install.ps1" -ReleaseRoot "%~dp0."
if errorlevel 1 (
  echo.
  echo Installation did not finish. Read the message above, then try Repair if needed.
  pause
)
