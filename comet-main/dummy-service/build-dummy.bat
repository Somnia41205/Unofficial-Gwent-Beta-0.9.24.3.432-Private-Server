@echo off
REM ===================================================================
REM  Build the wine-hardened GalaxyCommunication dummy service.
REM  Run from an "x64 Native Tools Command Prompt for VS" (so cl.exe is
REM  on PATH), or any prompt that has gcc/mingw. Output:
REM  GalaxyCommunication.exe in this folder.
REM ===================================================================
setlocal
cd /d "%~dp0"
set "SRC=communication_wine.c"
set "OUT=GalaxyCommunication.exe"

echo Building %OUT% from %SRC% ...
if exist "%OUT%" del /q "%OUT%"

where cl >nul 2>&1
if not errorlevel 1 goto try_msvc

where x86_64-w64-mingw32-gcc >nul 2>&1
if not errorlevel 1 goto try_mingw

where gcc >nul 2>&1
if not errorlevel 1 goto try_gcc

goto no_compiler

:try_msvc
echo Using MSVC cl.exe
cl /nologo /W3 /O2 "%SRC%" /Fe:"%OUT%" advapi32.lib user32.lib
if exist "%OUT%" goto ok_msvc
echo MSVC build failed.
goto try_mingw_after_msvc

:ok_msvc
del /q *.obj 2>nul
echo SUCCESS: %OUT% built with MSVC.
goto done

:try_mingw_after_msvc
where x86_64-w64-mingw32-gcc >nul 2>&1
if errorlevel 1 goto no_compiler
:try_mingw
echo Using x86_64-w64-mingw32-gcc
x86_64-w64-mingw32-gcc "%SRC%" -o "%OUT%" -ladvapi32 -mwindows -O2
if exist "%OUT%" goto ok_mingw
echo MinGW build failed.
goto no_compiler

:ok_mingw
echo SUCCESS: %OUT% built with MinGW.
goto done

:try_gcc
echo Using gcc
gcc "%SRC%" -o "%OUT%" -ladvapi32 -mwindows -O2
if exist "%OUT%" goto ok_gcc
echo gcc build failed.
goto no_compiler

:ok_gcc
echo SUCCESS: %OUT% built with gcc.
goto done

:no_compiler
echo.
echo ERROR: No working compiler found.
echo   - For MSVC: open "x64 Native Tools Command Prompt for VS" and run this again.
echo   - Or install MinGW-w64 / MSYS2 (mingw-w64-x86_64-gcc).
exit /b 1

:done
echo.
echo Next steps:
echo   1. Copy %OUT% over linux-bundle\GalaxyCommunication.exe
echo   2. Rebuild the launcher
echo   3. On the Deck once: flatpak run org.winehq.Wine sc delete GalaxyCommunication
endlocal
