@echo off
setlocal
rem 一次性安装本地出图环境（ComfyUI + 模型，全落 D:/AI/ComfyUI）
rem 断了就再双击一次，模型下载是断点续传的
cd /d "%~dp0"
if not exist "tools\setup_comfy.py" (
  echo [X] 找不到 tools\setup_comfy.py，确认 start.bat 这一层目录再跑
  pause
  exit /b 1
)
python tools\setup_comfy.py %*
echo.
echo 装完双击 comfy_up.bat 启动，然后 python tools\check_comfy.py 自检
pause
