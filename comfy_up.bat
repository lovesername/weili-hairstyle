@echo off
setlocal
rem ============================================================
rem  启动本地出图引擎 ComfyUI（装一次，之后每次跑这个）
rem  ComfyUI / 模型 / venv 全部在 D 盘：D:/AI/ComfyUI
rem ============================================================
set COMFY_ROOT=D:/AI/ComfyUI
set HF_ENDPOINT=https://hf-mirror.com
set HF_HOME=%COMFY_ROOT%\cache\hf
set PIP_CACHE_DIR=%COMFY_ROOT%\cache\pip

if not exist "%COMFY_ROOT%\main.py" (
  echo [!] 没装 ComfyUI，先跑一次安装：
  echo     python tools\setup_comfy.py
  echo     或双击 setup_comfy.bat
  pause
  exit /b 1
)

netstat -ano | findstr /r /c:":8188 .*LISTENING" >nul
if %errorlevel%==0 (
  echo [i] 8188 已在跑，直接开浏览器
  start "" http://127.0.0.1:8188
  exit /b 0
)

echo ============================================
echo   ComfyUI  http://127.0.0.1:8188
echo   首次出图要加载模型 20~40s，之后 4~8s/张
echo   关闭本窗口即停
echo ============================================
cd /d "%COMFY_ROOT%"
".venv\Scripts\python.exe" main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch --preview-method none
echo.
echo [X] ComfyUI 退出了，看上面的报错；显存不够就先关掉其他占显存的程序
pause
