@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo 关闭失败：找不到虚拟环境 %PYTHON%
    pause
    exit /b 3
)
"%PYTHON%" "%ROOT%scripts\service_manager.py" disable-autostart
pause
