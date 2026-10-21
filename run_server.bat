@echo off
chcp 936 >nul
setlocal enableextensions
cd /d "%~dp0backend"
title AI换发型-服务窗口别关

if not exist app.py (
  echo [X] 找不到 app.py，这个脚本要在 ai-hairstyle 目录里
  pause
  exit /b 1
)

python -m uvicorn app:app --host 0.0.0.0 --port 8899
echo.
echo 服务已退出，把上面的报错发给技术看
pause
