﻿@echo off
setlocal EnableExtensions DisableDelayedExpansion
title EKB 测试服 8511 - 停止
chcp 65001 >nul

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "MANAGER=%ROOT%scripts\service_manager.py"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%PYTHON%" (
    echo [错误] 找不到项目虚拟环境解释器：%PYTHON%
    set "EXIT_CODE=3"
    goto :finish
)

if not exist "%MANAGER%" (
    echo [错误] 找不到服务管理脚本：%MANAGER%
    set "EXIT_CODE=4"
    goto :finish
)

echo 正在停止工程知识库测试服（8511；不影响 8501 正式服）...
"%PYTHON%" "%MANAGER%" stop --staging
set "EXIT_CODE=%ERRORLEVEL%"

:finish
echo.
echo 按任意键关闭此窗口...
pause >nul
endlocal & exit /b %EXIT_CODE%
