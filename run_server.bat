@echo off
cd /d "%~dp0"
title MicroLoan Manager V1.5 Production Server
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found on this computer.
  echo Install Python 3 and try again.
  pause
  exit /b 1
)
echo Starting MicroLoan Manager V1.5 Production Server...
python app.py
pause
