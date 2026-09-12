@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ==================================================
echo  Competency DSS v22.9 - Stop Server
echo ==================================================
echo.

if exist ".server.pid" (
    set /p SERVER_PID=<".server.pid"
    if defined SERVER_PID (
        taskkill /PID !SERVER_PID! /T /F >nul 2>&1
        if not errorlevel 1 (
            del /q ".server.pid" >nul 2>&1
            echo Server dihentikan.
            pause
            exit /b 0
        )
    )
)

rem Fallback: only stop port 8000 when it responds as our Build v22.9 server.
powershell -NoProfile -Command "try { $r=Invoke-RestMethod -TimeoutSec 2 http://127.0.0.1:8000/api/build; if($r.build -eq 'v22.9'){exit 0}else{exit 1} } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo Server Competency DSS v22.9 tidak terdeteksi.
    if exist ".server.pid" del /q ".server.pid" >nul 2>&1
    pause
    exit /b 0
)

for /f "tokens=5" %%P in ('netstat -ano ^| findstr LISTENING ^| findstr ":8000"') do taskkill /PID %%P /T /F >nul 2>&1
if exist ".server.pid" del /q ".server.pid" >nul 2>&1
echo Server dihentikan.
pause
endlocal
