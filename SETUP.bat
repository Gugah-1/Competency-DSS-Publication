@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ==================================================
echo  Competency DSS v22.10.5 - Automatic Setup
echo ==================================================
echo.

echo [1/5] Mencari instalasi Python...
set "PY_EXE="
set "PY_ARGS="

rem Prefer Python Launcher if available. Python 3.12 is recommended,
rem but 3.11-3.14 are accepted for this prototype.
py -3.12 --version >nul 2>&1 && set "PY_EXE=py" && set "PY_ARGS=-3.12"
if not defined PY_EXE py -3.13 --version >nul 2>&1 && set "PY_EXE=py" && set "PY_ARGS=-3.13"
if not defined PY_EXE py -3.14 --version >nul 2>&1 && set "PY_EXE=py" && set "PY_ARGS=-3.14"
if not defined PY_EXE py -3.11 --version >nul 2>&1 && set "PY_EXE=py" && set "PY_ARGS=-3.11"
if not defined PY_EXE py -3 --version >nul 2>&1 && set "PY_EXE=py" && set "PY_ARGS=-3"
if not defined PY_EXE python --version >nul 2>&1 && set "PY_EXE=python" && set "PY_ARGS="

if not defined PY_EXE (
    echo.
    echo Python belum ditemukan. Mencoba memasang Python 3.12 64-bit otomatis...
    where winget >nul 2>&1
    if errorlevel 1 goto :PYTHON_INSTALL_UNAVAILABLE

    winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements --silent --disable-interactivity
    if errorlevel 1 goto :PYTHON_INSTALL_FAIL

    rem Refresh common per-user and all-user install locations in this process.
    if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY_EXE=%LocalAppData%\Programs\Python\Python312\python.exe" && set "PY_ARGS="
    if not defined PY_EXE if exist "%ProgramFiles%\Python312\python.exe" set "PY_EXE=%ProgramFiles%\Python312\python.exe" && set "PY_ARGS="
    if not defined PY_EXE py -3.12 --version >nul 2>&1 && set "PY_EXE=py" && set "PY_ARGS=-3.12"
    if not defined PY_EXE python --version >nul 2>&1 && set "PY_EXE=python" && set "PY_ARGS="
    if not defined PY_EXE goto :PYTHON_INSTALL_FAIL
)

"%PY_EXE%" %PY_ARGS% --version

echo.
echo [2/5] Membuat virtual environment khusus aplikasi...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" --version >nul 2>&1
    if errorlevel 1 (
        echo Virtual environment lama tidak kompatibel. Membuat ulang environment lokal...
        rmdir /s /q ".venv"
    ) else (
        echo Virtual environment sudah tersedia dan valid.
    )
)
if not exist ".venv\Scripts\python.exe" (
    "%PY_EXE%" %PY_ARGS% -m venv .venv
    if errorlevel 1 (
        echo.
        echo Gagal membuat virtual environment.
        echo Pastikan instalasi Python lengkap dan modul venv tersedia.
        pause
        exit /b 1
    )
)

echo.
echo [3/5] Menginstal dependency aplikasi...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :INSTALL_FAIL
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :INSTALL_FAIL

echo.
echo [4/5] Memeriksa file dan database...
if not exist "api_server.py" goto :FILE_FAIL
if not exist "web\index.html" goto :FILE_FAIL
if not exist "data\competency_dss.db" goto :FILE_FAIL
".venv\Scripts\python.exe" -c "from database import production_preflight; r=production_preflight(); print(r['message']); raise SystemExit(0 if r.get('ready') else 1)"
if errorlevel 1 (
    echo Database tidak lolos pemeriksaan awal.
    pause
    exit /b 1
)

echo Menjalankan migration security v22.8 jika diperlukan...
".venv\Scripts\python.exe" migration_v22_7_to_v22_8.py
if errorlevel 1 (
    echo Migration v22.8 gagal. Jangan lanjutkan penggunaan sebelum database diperiksa.
    pause
    exit /b 1
)
echo Menjalankan migration reporting v22.9 jika diperlukan...
".venv\Scripts\python.exe" migration_v22_8_to_v22_9.py
if errorlevel 1 (
    echo Migration v22.9 gagal. Jangan lanjutkan penggunaan sebelum database diperiksa.
    pause
    exit /b 1
)

echo.
echo [5/5] Memeriksa backend FastAPI...
".venv\Scripts\python.exe" -c "from api_server import app; print('FastAPI OK -', app.title)"
if errorlevel 1 (
    echo Backend gagal dimuat. Periksa pesan error di atas.
    pause
    exit /b 1
)

echo.
echo ==================================================
echo  SETUP SELESAI
echo ==================================================
echo Selanjutnya cukup double-click START.bat.
echo Laptop ini akan bertindak sebagai server selama aplikasi digunakan.
echo User lain di LAN hanya membutuhkan browser.
echo Login sekarang WAJIB. Baca FIRST_LOGIN_CREDENTIALS.txt untuk login pertama.
echo Hapus file credential setelah password Supervisor TCD dan HRD sudah diganti.
echo.
if /i not "%~1"=="/AUTO" pause
exit /b 0

:PYTHON_INSTALL_UNAVAILABLE
echo.
echo Python belum tersedia dan Windows Package Manager ^(winget^) tidak ditemukan.
echo Paket ini tidak memerlukan VS Code atau Anaconda, tetapi Python standar dibutuhkan pada laptop server.
echo Hubungi IT untuk mengaktifkan App Installer/winget atau memasang Python 3.12 64-bit.
echo Setelah itu cukup jalankan START.bat kembali.
if /i not "%~1"=="/AUTO" pause
exit /b 1

:PYTHON_INSTALL_FAIL
echo.
echo Instalasi otomatis Python tidak berhasil.
echo Pastikan laptop terhubung internet dan instalasi aplikasi tidak diblokir kebijakan perusahaan.
echo Tidak ada data aplikasi yang diubah. Hubungi IT bila kebijakan instalasi memerlukan hak administrator.
if /i not "%~1"=="/AUTO" pause
exit /b 1

:INSTALL_FAIL
echo.
echo Gagal menginstal dependency.
echo Pastikan laptop terhubung internet dan pip tidak diblokir kebijakan perusahaan.
echo Jika jaringan perusahaan membatasi instalasi, hubungi IT untuk pemasangan dependency.
if /i not "%~1"=="/AUTO" pause
exit /b 1

:FILE_FAIL
echo.
echo File aplikasi tidak lengkap. Pastikan folder hasil ZIP diekstrak seluruhnya.
if /i not "%~1"=="/AUTO" pause
exit /b 1
