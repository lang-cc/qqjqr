@echo off
chcp 65001 >nul
echo 正在停止 AstrBot（监听 6185 的进程）...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":6185" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a
)
echo 正在停止排队代理（监听 8878 的进程）...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8878" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a
)
echo 已停止。QQ / SnowLuma 不受影响，它们只是连不上 6199 而已。
timeout /t 2 >nul
