@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo   ASRTools Nuitka 打包脚本
echo ========================================
echo.

REM 检查 ffmpeg.exe 是否存在
if not exist "ffmpeg.exe" (
    echo [警告] 未找到 ffmpeg.exe
    echo 请将 ffmpeg.exe 放在当前目录，或修改打包脚本中的 --include-data-file 参数
    echo.
)

REM 检查图标文件是否存在
if not exist "resources\app_icon.ico" (
    echo [错误] 未找到图标文件 resources\app_icon.ico
    echo 请先运行: python generate_icon.py
    pause
    exit /b 1
)

echo [1/3] 检查环境...
venv_asr\Scripts\python.exe --version
venv_asr\Scripts\python.exe -m nuitka --version
echo.

echo [2/3] 开始打包...
echo 这可能需要几分钟时间，请耐心等待...
echo.

venv_asr\Scripts\python.exe -m nuitka --standalone ^
    --onefile ^
    --windows-icon-from-ico=resources/app_icon.ico ^
    --windows-console-mode=disable ^
    --windows-product-name="ASRTools" ^
    --windows-file-description="ASR语音识别工具" ^
    --windows-company-name="ASRTools" ^
    --output-dir=dist ^
    --enable-plugin=pyqt5 ^
    --include-data-file=ffmpeg.exe=ffmpeg.exe ^
    --include-data-dir=bk_asr=bk_asr ^
    --include-data-dir=resources=resources ^
    --include-package=qfluentwidgets ^
    --include-package=bk_asr ^
    --remove-output ^
    --lto=yes ^
    asr_gui.py

if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo [3/3] 打包成功！
    echo ========================================
    echo.
    echo 输出文件: dist\asr_gui.exe
    echo.
    
    if exist "dist\asr_gui.exe" (
        for %%A in ("dist\asr_gui.exe") do (
            echo 文件大小: %%~zA 字节
        )
    )
    echo.
) else (
    echo.
    echo ========================================
    echo [错误] 打包失败！
    echo ========================================
    echo 请检查上方的错误信息
    echo.
)

pause
