@echo off
setlocal
cd /d "%~dp0"
where pyw >nul 2>nul
if errorlevel 1 (
  echo Python for Windows is required. Install it from python.org.
  pause
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg is required. Install FFmpeg and add it to PATH.
  pause
  exit /b 1
)
where ffprobe >nul 2>nul
if errorlevel 1 (
  echo FFprobe is required. Install FFmpeg and add it to PATH.
  pause
  exit /b 1
)
start "" pyw -3 "%~dp0desktop_app.pyw"
