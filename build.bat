@echo off
setlocal
chcp 65001 >nul 2>&1

if "%~1"=="" (
    echo [错误] 必须显式提供 FFmpeg 8.1.2 路径。
    echo 用法: build.bat "D:\path\to\ffmpeg.exe"
    exit /b 2
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_release.ps1" -FFmpegPath "%~1"
exit /b %errorlevel%
