# ai写的天才的自动解密解压机器

[English version below](#english)

一款 **Windows / Linux / macOS 通用**的「批量改后缀 + 压缩包解密解压」图形化工具。
基于 Python + tkinter 构建；所有解压 / 校验均调用**原生工具**（UnRAR / 7-Zip），
对大文件远快于纯 Python 库，且 Windows 已随包分发所需工具，开箱即用。


## ✨ 功能特性

1. **批量改后缀**：选文件夹 / 递归子文件夹，按文件真实格式（magic 头）把误改后缀的
   压缩包改名到正确后缀，并汇报每个被识别压缩包的真实格式。此功能不做任何密码验证 / 解压，
   速度快，与「尝试密码本」完全分离。
2. **压缩包检测（含尝试密码本）**：扫描文件夹，识别真实格式，判断误改后缀 / 损坏 / 加密；
   内置「尝试密码本」按钮——仅用密码本把每个加密压缩包试一遍（不破解、不解压、多卷去重），
   命中即弹窗提示。速度只取决于「密码本长度 × 压缩包数」。
3. **密码本 & 破解解压**：可编辑密码本、递归解压嵌套压缩包、检测嵌套、手动输入密码、
   成功后记录密码、损坏自动换格式——真正的破解 / 解压入口。

支持格式：**ZIP / 7Z / RAR / RAR5**、多卷压缩包、中文密码、带密码压缩包。

## 🖥️ 环境要求

- Python 3.10+
- **Windows**：已随包内置 `tools/UnRAR.exe` 与 `tools/7za.exe`，开箱即用。
- **Linux**：需系统安装 `unrar` 与 `p7zip`（如 `sudo apt install unrar p7zip-full`）。
- **macOS**：需系统安装 `unrar` 与 `p7zip`（如 `brew install unrar p7zip`）。

## 🚀 快速开始（源码运行）

```bash
git clone <repo-url>
cd <repo>
pip install -r requirements.txt
python main.py
```

> Windows 也可直接双击 `run.bat`（若 `passwords.txt` 不存在，复制 `passwords.example.txt` 为 `passwords.txt` 后编辑）。

## 📦 单文件打包（PyInstaller）

本工具使用 PyInstaller `--onefile` 打包为**单文件可执行程序**。
**注意：PyInstaller 无法跨平台编译，各平台的可执行文件必须在对应系统上构建。**

| 平台 | 构建脚本 | 产物 |
| --- | --- | --- |
| Windows | `build_windows.bat` | `dist/解密解压机器.exe` |
| Linux | `bash build_linux.sh` | `dist/解密解压机器` |
| macOS | `bash build_mac.sh` | `dist/解密解压机器` |

构建脚本会自动捆绑依赖；Windows 下还会把 `tools/` 内的原生工具打进单文件。
Linux / macOS 需按上文「环境要求」先安装 `unrar` 与 `p7zip`。

## 📁 目录结构

```
.
├── main.py                # GUI 主程序
├── archivelib.py          # 核心逻辑：格式识别 / 检测 / 密码尝试 / 解压 / 递归
├── powersave.py           # 低功耗（限 CPU）辅助
├── tools/                 # Windows 原生工具（UnRAR.exe / 7za.exe / dll）
├── assets/                # logo 等资源
├── passwords.example.txt  # 密码本模板（复制为 passwords.txt 使用）
├── requirements.txt
├── build_windows.bat / build_linux.sh / build_mac.sh
├── README.md / LICENSE / .gitignore
└── run.bat
```

## ⚠️ 免责声明

本工具仅用于**你自己拥有合法授权的压缩包**（如忘记密码的私人备份）。
请勿用于未经授权的解密 / 破解行为，由此产生的一切法律责任由使用者自行承担。

## 📄 许可证

[MIT](LICENSE)

---

## English

A cross-platform GUI tool for **batch extension renaming + archive decrypting/extracting**
(Windows / Linux / macOS). Built with Python + tkinter; all extraction/verification calls
native tools (UnRAR / 7-Zip) for speed.

### Features
- **Batch rename** by real format (magic bytes); no password checking, fast.
- **Archive detection** with a "try password dictionary" button (no cracking, multi-volume
  dedupe; pops up when a password is found).
- **Password dictionary & real extraction**: editable dictionary, nested recursion,
  manual password, auto format-swap on corruption.

Supports ZIP / 7Z / RAR / RAR5, multi-volume archives, non-ASCII passwords.

### Requirements
- Python 3.10+
- Windows: bundled `tools/UnRAR.exe` + `tools/7za.exe`.
- Linux: `unrar` + `p7zip` (e.g. `sudo apt install unrar p7zip-full`).
- macOS: `unrar` + `p7zip` (e.g. `brew install unrar p7zip`).

### Run from source
```bash
pip install -r requirements.txt
python main.py
```

### Build a single-file executable
PyInstaller `--onefile` (cannot cross-compile). Use `build_windows.bat` / `build_linux.sh` /
`build_mac.sh` on the respective OS.

### Disclaimer
For archives you legally own/authorize only. The author is not responsible for misuse.

### License
[MIT](LICENSE)
