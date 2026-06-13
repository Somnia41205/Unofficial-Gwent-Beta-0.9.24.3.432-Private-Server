@echo off
REM Build GwentServerHost.exe — self-contained same-PC Gwent private server.
REM Requires: pip install pyinstaller
REM Output: dist\GwentServerHost.exe
REM
REM Ships NO game data. The host extracts card definitions from their own
REM client at setup time; static files ship blank.

echo Building Gwent Server Host...
echo.

REM --- Verify required pieces exist ---
set MISSING=0
if not exist "deploy\server.py"   (echo ERROR: deploy\server.py not found & set MISSING=1)
if not exist "deploy\broker.py"   (echo ERROR: deploy\broker.py not found & set MISSING=1)
if not exist "deploy\relay.py"    (echo ERROR: deploy\relay.py not found & set MISSING=1)
if not exist "deploy\db.py"       (echo ERROR: deploy\db.py not found & set MISSING=1)
if not exist "deploy\server_host_main.py" (echo ERROR: deploy\server_host_main.py not found & set MISSING=1)
if not exist "deploy\server_host_gui.py" (echo ERROR: deploy\server_host_gui.py not found & set MISSING=1)
if not exist "Nginx\nginx.exe"    (echo ERROR: Nginx\nginx.exe not found & set MISSING=1)
if not exist "host_nginx.conf"    (echo ERROR: host_nginx.conf not found & set MISSING=1)
if not exist "deploy\static"      (echo ERROR: deploy\static folder not found & set MISSING=1)

if %MISSING% EQU 1 (
    echo.
    echo Missing required files. Build aborted.
    pause
    exit /b 1
)

REM relay.py needs the third-party "websockets" package bundled.
python -c "import websockets" 2>NUL
if errorlevel 1 (
    echo ERROR: the "websockets" package is not installed in this Python.
    echo        Install it first:  pip install websockets
    pause
    exit /b 1
)

pyinstaller --clean --noconfirm GwentServerHost.spec

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo  Build successful!
    echo  Output: dist\GwentServerHost.exe
    echo ============================================================
    echo.
    echo Host usage:
    echo   1^) GwentServerHost.exe --setup "C:\path\to\Gwent The Witcher Card Game"
    echo      ^(extracts card data from your own client, lays down blank static files^)
    echo   2^) GwentServerHost.exe
    echo      ^(starts server + nginx; share your LAN IP with a friend^)
    echo.
) else (
    echo.
    echo Build failed. Make sure PyInstaller is installed:  pip install pyinstaller
)

pause
