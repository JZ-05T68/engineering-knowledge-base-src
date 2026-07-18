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
    set "EXIT_CODE=3"
    goto :finish
)

if not exist "%MANAGER%" (
    echo [错误] 找不到服务管理脚本：
    echo %MANAGER%
    set "EXIT_CODE=4"
    goto :finish
)

echo 正在检查工程知识库运行状态...
"%PYTHON%" "%MANAGER%" status
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo [错误] 状态检查发现异常，退出码：%EXIT_CODE%
)

:finish
echo.
echo 按任意键关闭此窗口...
pause >nul
endlocal & exit /b %EXIT_CODE%
