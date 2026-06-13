@echo off
REM Build the Gwent Beta launcher into a single exe using PyInstaller.
REM Requires: pip install pyinstaller
REM Output: dist\GwentBetaLauncher.exe

echo Building Gwent Beta Launcher...
echo.

REM Paths to bundled files
set COMM_SERVICE=commservice.py
set DNS_PROXY=dns_proxy.py
set HTTPS_PROXY=https_proxy.py
set CERT_FILE=Nginx\conf\fake.crt
set KEY_FILE=Nginx\conf\fake.key
set GALAXY_COMM=comet-main\dummy-service\GalaxyCommunication.exe
set MOD_DLL=GwentBetaRestorationMod\GwentBetaRestorationMod.dll
set SETTINGS_DIR=settings

REM Verify all required files exist
set MISSING=0
if not exist "%COMM_SERVICE%" (echo ERROR: %COMM_SERVICE% not found & set MISSING=1)
if not exist "%DNS_PROXY%" (echo ERROR: %DNS_PROXY% not found & set MISSING=1)
if not exist "%HTTPS_PROXY%" (echo ERROR: %HTTPS_PROXY% not found & set MISSING=1)
if not exist "%CERT_FILE%" (echo ERROR: %CERT_FILE% not found & set MISSING=1)
if not exist "%KEY_FILE%" (echo ERROR: %KEY_FILE% not found & set MISSING=1)
if not exist "%GALAXY_COMM%" (echo ERROR: %GALAXY_COMM% not found & set MISSING=1)
if not exist "%MOD_DLL%" (echo ERROR: %MOD_DLL% not found & set MISSING=1)
if not exist "%SETTINGS_DIR%\config.json" (echo ERROR: %SETTINGS_DIR%\config.json not found & set MISSING=1)
if not exist "%SETTINGS_DIR%\Launch.cfg" (echo ERROR: %SETTINGS_DIR%\Launch.cfg not found & set MISSING=1)

if %MISSING% EQU 1 (
    echo.
    echo Missing required files. Build aborted.
    pause
    exit /b 1
)

REM --- Resolve the server host (kept out of source) ----------------------
REM Priority: GWENT_SERVER_HOST env var, else the private server.txt file.
REM The result is written to server_host.txt and bundled into the exe.
if not "%GWENT_SERVER_HOST%"=="" (
    >server_host.txt echo %GWENT_SERVER_HOST%
) else if exist server.txt (
    copy /y server.txt server_host.txt >nul
) else (
    echo ERROR: no server host configured.
    echo   Set GWENT_SERVER_HOST, or create server.txt with one line: your.host.or.ip
    echo   ^(edit server.txt and put your server's url or ip on the first line^)
    pause
    exit /b 1
)

pyinstaller --onefile --noconsole --name GwentBetaLauncher ^
    --uac-admin ^
    --add-data "%COMM_SERVICE%;." ^
    --add-data "%DNS_PROXY%;." ^
    --add-data "%HTTPS_PROXY%;." ^
    --add-data "%CERT_FILE%;." ^
    --add-data "%KEY_FILE%;." ^
    --add-data "%GALAXY_COMM%;." ^
    --add-data "%MOD_DLL%;." ^
    --add-data "%SETTINGS_DIR%;settings" ^
    --add-data "server_host.txt;." ^
    --hidden-import=socketserver ^
    --hidden-import=http.server ^
    --hidden-import=http.client ^
    launcher.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo  Build successful!
    echo  Output: dist\GwentBetaLauncher.exe
    echo ============================================================
    echo.
    echo The exe is fully self-contained. No Python required.
    echo Users just download, run, and play.
    echo.
) else (
    echo.
    echo Build failed. Make sure PyInstaller is installed:
    echo   pip install pyinstaller
)

pause
