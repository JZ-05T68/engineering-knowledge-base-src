﻿@echo off
chcp 65001 >nul
set "STABLE_ROOT=%EKB_STABLE_RUNTIME_ROOT%"
if "%STABLE_ROOT%"=="" set "STABLE_ROOT=D:\Projects\ekb-runtime\stable"
set "SNAPSHOT_PYTHON=%STABLE_ROOT%\.venv\Scripts\pythonw.exe"
set "SNAPSHOT_MANAGER=%STABLE_ROOT%\scripts\service_manager.py"
if not exist "%SNAPSHOT_MANAGER%" (
    echo 关闭失败：找不到 8501 稳定运行时快照：%SNAPSHOT_MANAGER%
    pause
    exit /b 5
)
"%SNAPSHOT_PYTHON%" "%SNAPSHOT_MANAGER%" disable-autostart
pause
