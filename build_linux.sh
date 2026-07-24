#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if ! command -v pyinstaller >/dev/null 2>&1; then
  echo "未找到 pyinstaller，请先安装： pip install pyinstaller"
  exit 1
fi
echo "提示：本程序依赖系统 unrar 与 p7zip，请先安装："
echo "  sudo apt install unrar p7zip-full   # Debian/Ubuntu"
echo "  sudo dnf install unrar p7zip         # Fedora"
pyinstaller --onefile --windowed \
  --name "解密解压机器" \
  --hidden-import archivelib \
  --hidden-import powersave \
  --hidden-import sv_ttk \
  --hidden-import py7zr \
  --hidden-import pyzipper \
  --hidden-import PIL \
  --hidden-import PIL.ImageTk \
  --add-data "assets:assets" \
  main.py
echo "完成：dist/解密解压机器"
