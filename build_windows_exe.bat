@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1"
if errorlevel 1 (
  echo Windows build failed. Please keep the text above for troubleshooting.
  pause
  exit /b 1
)
echo Done: %~dp0dist\FootageMaker\FootageMaker.exe
pause
