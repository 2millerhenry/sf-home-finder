@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0payload\tools\repair.ps1" -ReleaseRoot "%~dp0."
if errorlevel 1 pause
