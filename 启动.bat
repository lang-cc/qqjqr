@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title AstrBot - qqjqr

echo ==========================================
echo   AstrBot 启动中...
echo   面板: http://127.0.0.1:6185
echo   OneBot 反向 WS: ws://127.0.0.1:6199/ws
echo   关闭这个窗口 = 停止服务
echo ==========================================

rem ---- 先起本地排队代理（把网关并发压到 2，避免 429）----
netstat -ano | findstr ":8878" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [代理] 启动排队代理 127.0.0.1:8878 ...
    start "" /b ".venv\Scripts\python.exe" "tools\gw_proxy.py"
    timeout /t 2 >nul
) else (
    echo [代理] 已在运行，跳过
)

".venv\Scripts\astrbot.exe" run
pause
