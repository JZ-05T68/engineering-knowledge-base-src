@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "MANAGER=%ROOT%scripts\service_manager.py"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%PYTHON%" (
    echo [错误] 找不到项目虚拟环境解释器：
    echo %PYTHON%
    echo 请先按照 README 完成首次安装。
    set "EXIT_CODE=3"
    goto :finish
)

if not exist "%MANAGER%" (
    echo [错误] 找不到服务管理脚本：
    echo %MANAGER%
    echo 请确认项目文件完整，且 scripts 目录未被移动。
    set "EXIT_CODE=4"
    goto :finish
)

echo 正在启动工程知识库...
"%PYTHON%" "%MANAGER%" start
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo [错误] 启动命令执行失败，退出码：%EXIT_CODE%
) else (
    echo 启动命令执行完成。
)

:finish
echo.
echo 按任意键关闭此窗口...
pause >nul
endlocal & exit /b %EXIT_CODE%
