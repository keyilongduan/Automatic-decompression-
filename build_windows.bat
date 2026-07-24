@echo off
cd /d "%~dp0"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if not exist "%PY%" (
  echo 未找到 Python，请先安装或激活虚拟环境。
  pause
  exit /b 1
)
echo 正在用 PyInstaller 打包 Windows 单文件程序...
"%PY%" -m PyInstaller --onefile --windowed ^
  --name "解密解压机器" ^
  --add-data "tools/UnRAR.exe;tools" ^
  --add-data "tools/7za.exe;tools" ^
  --add-data "tools/7za.dll;tools" ^
  --add-data "tools/7zxa.dll;tools" ^
  --add-data "assets;assets" ^
  --hidden-import archivelib ^
  --hidden-import powersave ^
  --hidden-import sv_ttk ^
  --hidden-import py7zr ^
  --hidden-import pyzipper ^
  --hidden-import PIL ^
  --hidden-import PIL.ImageTk ^
  main.py
echo 完成：dist\解密解压机器.exe
pause
