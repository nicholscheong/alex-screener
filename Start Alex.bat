@echo off
chcp 65001 >nul
title Alex Dashboard
cd /d "%~dp0"

if not exist "%USERPROFILE%\Desktop\Start Alex.lnk" (
  echo   Creating a desktop shortcut for next time...
  powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%USERPROFILE%\Desktop\Start Alex.lnk'); $s.TargetPath='%~f0'; $s.WorkingDirectory='%~dp0'; if (Test-Path '%~dp0favicon.ico') { $s.IconLocation='%~dp0favicon.ico' }; $s.Save()" >nul 2>nul
  echo.
)

echo.
echo   Starting the Alex dashboard ...
echo   Your browser will open http://127.0.0.1:8787
echo   Keep this window open; closing it stops the server.
echo.

set "PYEXE="
for %%P in (py "py -3" python python3) do (
  if not defined PYEXE (
    %%~P -c "import sys" >nul 2>nul && set "PYEXE=%%~P"
  )
)

if not defined PYEXE (
  echo   [ERROR] No working Python 3 found.
  echo   Install it from https://www.python.org/downloads/ then retry.
  echo.
  pause
  exit /b 1
)

echo   Using interpreter: %PYEXE%
echo.
%PYEXE% server.py

echo.
echo   Server stopped. Press any key to close.
pause >nul
