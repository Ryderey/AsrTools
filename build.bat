@echo off
setlocal
chcp 65001 >nul 2>&1

pushd "%~dp0"
if "%~1"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_build.ps1"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_build.ps1" -FFmpegPath "%~1"
)
set "build_result=%errorlevel%"
popd
if not "%build_result%"=="0" echo Unable to dispatch background build. See the error above.
pause
exit /b %build_result%
