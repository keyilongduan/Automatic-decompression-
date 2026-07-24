@echo off
cd /d "%~dp0"
set "PY="
if exist "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
  set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
) else if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
echo Starting 解密解压机器...
"%PY%" main.py
if errorlevel 1 pause
