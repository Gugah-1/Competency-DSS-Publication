@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ==================================================
echo  Competency DSS v22.10.5 - Start Server
echo ==================================================
echo.

rem START is also the first-time installer entry point. A copied .venv is not
rem portable because it points to the Python installation of the source PC,
rem therefore test the interpreter instead of only checking that the file exists.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" --version >nul 2>&1
)
if not exist ".venv\Scripts\python.exe" goto :FIRST_SETUP
".venv\Scripts\python.exe" --version >nul 2>&1
if errorlevel 1 goto :FIRST_SETUP
goto :SETUP_READY

:FIRST_SETUP
echo Setup awal diperlukan. Menjalankan instalasi otomatis...
call "%~dp0SETUP.bat" /AUTO
if errorlevel 1 (
    echo.
    echo Setup otomatis belum berhasil. Baca pesan di atas atau hubungi IT.
    pause
    exit /b 1
)

:SETUP_READY
if not exist "api_server.py" (
    echo api_server.py tidak ditemukan.
    pause
    exit /b 1
)
if not exist "web\index.html" (
    echo web\index.html tidak ditemukan.
    pause
    exit /b 1
)
if not exist "data\competency_dss.db" (
    echo data\competency_dss.db tidak ditemukan.
    pause
    exit /b 1
)

rem If our server is already active, do not start a duplicate process.
powershell -NoProfile -Command "try { $r=Invoke-RestMethod -TimeoutSec 2 http://127.0.0.1:8000/api/build; if($r.build -eq 'v22.10.5'){exit 0}else{exit 1} } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :READY

rem Do not kill an unrelated application that happens to use port 8000.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr LISTENING ^| findstr ":8000"') do (
    echo Port 8000 sedang digunakan oleh proses lain dengan PID %%P.
    echo Tutup aplikasi yang menggunakan port 8000 atau hubungi IT.
    pause
    exit /b 1
)

echo [1/3] Menjalankan FastAPI...
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$p=Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','api_server:app','--host','0.0.0.0','--port','8000' -WorkingDirectory '%~dp0' -WindowStyle Minimized -PassThru; $p.Id"`) do set "SERVER_PID=%%P"
if defined SERVER_PID echo !SERVER_PID!>".server.pid"

echo [2/3] Menunggu server siap...
for /l %%i in (1,1,30) do (
    powershell -NoProfile -Command "try { $r=Invoke-RestMethod -TimeoutSec 1 http://127.0.0.1:8000/health; if($r.status -eq 'ok'){exit 0}else{exit 1} } catch { exit 1 }" >nul 2>&1
    if not errorlevel 1 goto :READY
    timeout /t 1 /nobreak >nul
)

echo.
echo Server tidak merespons setelah 30 detik.
echo Jalankan DIAGNOSE.bat untuk pemeriksaan dasar.
pause
exit /b 1

:READY
echo [3/3] Aplikasi siap.
echo.

rem Open dashboard FIRST. LAN-IP detection must never block local use.
set "DASHBOARD_URL=http://127.0.0.1:8000/?build=v22.10.5"
echo Membuka dashboard di browser...
start "" "!DASHBOARD_URL!" >nul 2>&1
if errorlevel 1 explorer.exe "!DASHBOARD_URL!" >nul 2>&1

echo LOCAL   : !DASHBOARD_URL!

rem Detect a usable IPv4 using ipconfig. This is intentionally simpler than
rem Get-NetRoute/Get-NetIPAddress because some Windows/network policies can
rem make those PowerShell cmdlets wait for a long time.
set "LAN_IP="
for /f "tokens=2 delims=:" %%I in ('ipconfig ^| findstr /i "IPv4"') do (
    set "CANDIDATE=%%I"
    set "CANDIDATE=!CANDIDATE: =!"
    if defined CANDIDATE (
        if /i not "!CANDIDATE!"=="127.0.0.1" (
            if /i not "!CANDIDATE:~0,8!"=="169.254." (
                set "LAN_IP=!CANDIDATE!"
                goto :IP_DONE
            )
        )
    )
)

:IP_DONE
if defined LAN_IP (
    echo NETWORK : http://!LAN_IP!:8000/?build=v22.10.5
    echo.
    echo Perangkat lain pada jaringan LAN/Wi-Fi yang sama dapat membuka alamat NETWORK.
) else (
    echo NETWORK : IP LAN tidak terdeteksi otomatis.
    echo Jalankan ipconfig untuk melihat alamat IPv4 laptop server.
)

echo.
echo Dashboard seharusnya sudah terbuka di browser.
echo Login WAJIB. Untuk login pertama lihat FIRST_LOGIN_CREDENTIALS.txt.
echo Jika browser tidak terbuka, salin alamat LOCAL di atas ke browser.
echo Jangan matikan atau sleep laptop server selama aplikasi digunakan.
echo Untuk menghentikan aplikasi gunakan STOP.bat.
echo Jika akses dari perangkat lain diblokir, minta IT memeriksa Windows Firewall TCP port 8000.
echo.
pause
endlocal
