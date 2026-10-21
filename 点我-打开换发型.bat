@echo off
chcp 936 >nul
setlocal enableextensions
cd /d "%~dp0"
title AI 一键换发型

rem 服务已经在跑就直接开网页
curl -s -m 2 -o nul http://127.0.0.1:8899/api/health
if not errorlevel 1 goto OPEN

echo 正在启动服务，请等 10 秒...
start "AI换发型-服务窗口别关" /min "%~dp0run_server.bat"

set /a n=0
:WAIT
timeout /t 1 >nul
curl -s -m 2 -o nul http://127.0.0.1:8899/api/health
if not errorlevel 1 goto OPEN
set /a n+=1
if %n% lss 30 goto WAIT
echo [X] 启动失败：看任务栏那个最小化的服务窗口里的红色报错
pause
exit /b 1

:OPEN
start "" http://127.0.0.1:8899/
exit /b 0
