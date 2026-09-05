﻿@echo off
setlocal EnableExtensions DisableDelayedExpansion
title EKB 正式服 8501（Stable Runtime）
chcp 65001 >nul

set "ROOT=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "STABLE_ROOT=%EKB_STABLE_RUNTIME_ROOT%"
if "%STABLE_ROOT%"=="" set "STABLE_ROOT=D:\Projects\ekb-runtime\stable"
set "SNAPSHOT_PYTHON=%STABLE_ROOT%\.venv\Scripts\python.exe"
set "SNAPSHOT_MANAGER=%STABLE_ROOT%\scripts\service_manager.py"

if not exist "%SNAPSHOT_MANAGER%" (
    echo [错误] 找不到 8501 稳定运行时快照：
    echo %SNAPSHOT_MANAGER%
    echo 为了保护正式环境，8501 不再直接运行开发工作区。
    echo 请先在开发工作区执行：python scripts\export_stable_runtime.py
    set "EXIT_CODE=5"
    goto :finish
)

if not exist "%SNAPSHOT_PYTHON%" (
    echo [错误] 稳定运行时缺少解释器：%SNAPSHOT_PYTHON%
    set "EXIT_CODE=3"
    goto :finish
)

echo 正在停止工程知识库正式服（8501）...
"%SNAPSHOT_PYTHON%" "%SNAPSHOT_MANAGER%" stop
set "EXIT_CODE=%ERRORLEVEL%"

:finish
echo.
echo 按任意键关闭此窗口...
pause >nul
endlocal & exit /b %EXIT_CODE%
