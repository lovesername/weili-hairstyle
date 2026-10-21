@echo off
setlocal enableextensions
rem ============================================================
rem  AI 一键换发 - 后端启动（8899）
rem  注意：本文件必须保存为 CRLF + GBK，改编码会导致 cmd 拆行报
rem        "'st' 不是内部或外部命令" 和 "Could not import module app"
rem ============================================================
cd /d "%~dp0"

if not exist "backend\app.py" (
  echo [X] 当前目录不是 ai-hairstyle 项目根目录，找不到 backend\app.py
  echo     当前目录：%CD%
  echo     把 start.bat 放回 ai-hairstyle 目录下再双击
  echo.
  pause
  exit /b 1
)

if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  set "PY=py -3"
  where py >nul 2>nul
  if errorlevel 1 (
    echo [X] 找不到 python，先装 Python 3.10+ 并勾选 Add to PATH
    pause
    exit /b 1
  )
)

echo ============================================
echo   AI 一键换发   http://127.0.0.1:8899
echo   出图后端在页面「4 出图后端」里切换
echo   本地 ComfyUI 需要另开一个窗口跑 comfy_up.bat
echo   关闭本窗口即停服务
echo ============================================

cd /d "%~dp0backend"
%PY% -m uvicorn app:app --host 0.0.0.0 --port 8899

echo.
echo [X] 服务退出了，看上面的 ERROR 行：
echo     1) 端口被占：netstat -ano ^| findstr :8899    然后 taskkill /PID 那个号 /F
echo     2) 依赖没装：cd backend ^&^& %PY% -m pip install -r ..\requirements.txt
echo     3) 导入失败：确认是在 ai-hairstyle\backend 目录下跑
pause
