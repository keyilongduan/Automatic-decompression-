"""批量改后缀 & 压缩包解密解压工具 (GUI)。

功能：
  1. 批量改后缀：选文件夹 / 递归子文件夹 / 原后缀 -> 新后缀；按文件真实格式改名到
     正确后缀，并汇报每个被识别压缩包的真实格式。此标签不做任何密码验证/解压，
     保证改名本身快速、与“尝试密码本”完全分离。
  2. 压缩包检测：扫描文件夹，识别真实格式，判断误改后缀 / 损坏 / 加密。
     内置“尝试密码本”按钮——仅用密码本把每个加密压缩包试一遍（不破解、不解压、
     多卷去重），命中即弹窗提示；与改后缀功能分离，速度只取决于“密码本长度 × 压缩包数”。
  3. 密码本 & 破解解压：可编辑密码本、递归解压嵌套、检测嵌套、手动输入密码、
     成功后记录密码、损坏自动换格式（真正的破解/解压入口）。

界面：基于 sv-ttk（Windows 11 风格，自动回退到自定义 ttk 主题），高分屏 DPI 感知。
"""
import os
import sys
import json
import ctypes
import time
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

import archivelib
import powersave

try:
    import sv_ttk
    HAVE_SVTTK = True
except ImportError:
    HAVE_SVTTK = False

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
    try:
        _LOGO_RESAMPLE = Image.Resampling.LANCZOS
    except AttributeError:
        _LOGO_RESAMPLE = Image.LANCZOS
except ImportError:
    HAVE_PIL = False
    _LOGO_RESAMPLE = None


def resource_path(rel):
    """定位资源文件：开发模式用脚本所在目录；PyInstaller 单文件用 _MEIPASS。"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'passwords.txt')
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

DEFAULT_PASSWORDS = [
    '123456', '12345678', '111111', '000000', '888888', 'password', 'admin',
    '123456789', '123123', 'qwerty', '1q2w3e4r', 'abc123', 'root', '666666',
    '5201314', '1234567890', '1qaz2wsx', 'iloveyou', '1234', '12345',
]

BAD_STATUSES = ('格式不符(疑似误改后缀)', '损坏', '疑似损坏/非压缩文件')


# --------------------------------------------------------------------------
# 跨线程的“手动输入密码”请求器：工作线程阻塞等待，主线程弹窗返回。
# 弹窗前先自动尝试密码本；若命中则直接返回，不再打扰用户。
# --------------------------------------------------------------------------
class PasswordPrompter:
    def __init__(self, root, get_passwords, verify_password_cb):
        self.root = root
        self.get_passwords = get_passwords
        self.verify = verify_password_cb
        self.req = queue.Queue()
        self.res = queue.Queue()
        self._poll()

    def _poll(self):
        try:
            item = self.req.get_nowait()
        except queue.Empty:
            self.root.after(150, self._poll)
            return
        ans = self._ask(item)
        self.res.put(ans)
        self.root.after(150, self._poll)

    def _try_passwords(self, path, fmt):
        """在主线程中用密码本逐个尝试，返回命中的密码或 None。"""
        if not path or not fmt:
            return None
        passwords = self.get_passwords()
        for pwd in passwords:
            if self.verify(path, fmt, pwd):
                return pwd
        return None

    def _ask(self, item):
        path = item.get('path')
        fmt = item.get('fmt')
        prompt = item.get('prompt', '需要密码')
        name = item.get('name') or (os.path.basename(path) if path else '文件')

        # 先自动调用密码本尝试
        found = self._try_passwords(path, fmt)
        if found is not None:
            return found

        passwords = self.get_passwords()
        tried = len(passwords)
        return self._dialog(name, prompt, tried, path, fmt)

    def _dialog(self, name, prompt, tried, path, fmt):
        """自定义密码输入对话框，提示已尝试密码本并支持重试。"""
        win = tk.Toplevel(self.root)
        win.title('需要密码')
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        result = [None]

        ttk.Label(win, text=prompt).grid(row=0, column=0, columnspan=3, sticky='w', padx=12, pady=(12, 4))
        ttk.Label(win, text='文件: %s' % name, foreground='gray').grid(row=1, column=0, columnspan=3, sticky='w', padx=12, pady=(0, 4))

        hint = ttk.Label(win, text='已自动尝试密码本中的 %d 个密码，未命中。' % tried)
        hint.grid(row=2, column=0, columnspan=3, sticky='w', padx=12, pady=(0, 8))

        ttk.Label(win, text='密码:').grid(row=3, column=0, sticky='w', padx=12, pady=4)
        entry = ttk.Entry(win, width=42, show='*')
        entry.grid(row=3, column=1, columnspan=2, sticky='ew', padx=(0, 12), pady=4)
        entry.focus_set()

        def on_ok(event=None):
            result[0] = entry.get()
            win.destroy()

        def on_cancel():
            result[0] = None
            win.destroy()

        def on_retry():
            found = self._try_passwords(path, fmt)
            if found is not None:
                result[0] = found
                win.destroy()
                return
            messagebox.showinfo('提示', '密码本中仍未找到正确密码。', parent=win)

        btn_frame = ttk.Frame(win)
        btn_frame.grid(row=4, column=0, columnspan=3, sticky='e', padx=12, pady=(12, 12))
        ttk.Button(btn_frame, text='重新尝试密码本', command=on_retry).pack(side='left', padx=(0, 8))
        ttk.Button(btn_frame, text='取消', command=on_cancel).pack(side='right', padx=(4, 0))
        ttk.Button(btn_frame, text='确定', command=on_ok).pack(side='right', padx=(0, 4))

        win.protocol('WM_DELETE_WINDOW', on_cancel)
        win.bind('<Return>', on_ok)
        win.bind('<Escape>', lambda e: on_cancel())

        win.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - win.winfo_height()) // 2
        win.geometry('+%d+%d' % (x, y))

        win.wait_window()
        return result[0]

    def request(self, prompt, path=None, fmt=None):
        """工作线程调用：请求密码。若 path/fmt 提供，会先尝试密码本。"""
        item = {'prompt': prompt, 'path': path, 'fmt': fmt}
        if path:
            item['name'] = os.path.basename(path)
        self.req.put(item)
        return self.res.get()


class App:
    def __init__(self, root):
        self.root = root
        root.title('批量改后缀 & 压缩包解密解压工具')
        root.option_add('*Font', '{Segoe UI} 10')

        self._busy = False
        self.dark = tk.BooleanVar(value=False)
        self._power_applied = False
        self.progress_widgets = {}  # tab -> (Progressbar, Label)

        # 顶部标题栏（左侧带水墨刻字 logo）
        header = ttk.Frame(root)
        header.pack(fill='x', padx=0, pady=0)
        self._load_logo(root, header)
        ttk.Label(header, text='📦 批量改后缀 & 压缩包解密解压',
                  font=('Segoe UI', 13, 'bold')).pack(side='left', padx=12, pady=8)
        # 低功耗模式（默认开启，防止解密/解压时把机器跑过热死机）
        self.power_save = tk.BooleanVar(value=True)
        ttk.Checkbutton(header, text='🍃 低功耗模式', variable=self.power_save,
                        command=self._on_power_toggle).pack(side='right', padx=8, pady=6)
        self.theme_btn = ttk.Button(header, text='🌙 暗色', command=self.toggle_theme)
        self.theme_btn.pack(side='right', padx=12, pady=6)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True, padx=8, pady=8)

        self.build_rename_tab()
        self.build_detect_tab()
        self.build_crack_tab()

        self.prompter = PasswordPrompter(root, self.get_passwords, archivelib.verify_password)
        self.load_passwords()
        self._load_config()  # 恢复上次的选择（勾选项、文件夹路径、主题等）

    # ---------- 水墨刻字 logo ----------
    def _load_logo(self, root, header):
        """加载水墨刻字 logo：设为窗口图标，并在标题栏左侧显示小图。"""
        logo_path = resource_path(os.path.join('assets', 'logo.png'))
        icon_path = resource_path(os.path.join('assets', 'logo.ico'))
        # 窗口图标：优先用 iconphoto（跨平台）；Windows 也可 iconbitmap(.ico)
        try:
            if HAVE_PIL and os.path.isfile(logo_path):
                img = Image.open(logo_path).convert('RGBA')
                self._logo_img = ImageTk.PhotoImage(img)
                try:
                    root.iconphoto(True, self._logo_img)
                except Exception:
                    pass
            elif os.path.isfile(icon_path):
                try:
                    root.iconbitmap(icon_path)
                except Exception:
                    pass
        except Exception:
            pass
        # 标题栏左侧小 logo
        try:
            if HAVE_PIL and _LOGO_RESAMPLE is not None and os.path.isfile(logo_path):
                small = Image.open(logo_path).convert('RGBA').resize((40, 40), _LOGO_RESAMPLE)
                self._logo_small = ImageTk.PhotoImage(small)
                ttk.Label(header, image=self._logo_small).pack(side='left', padx=(12, 0), pady=6)
        except Exception:
            pass

    # ---------- 设置持久化（记住上次选择） ----------
    def _load_config(self):
        """启动时读取 config.json，恢复上次的勾选项与文件夹路径。"""
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            return

        def b(var, key, default=False):
            var.set(bool(cfg.get(key, default)))

        def s(var, key):
            v = cfg.get(key)
            if isinstance(v, str):
                var.set(v)

        b(self.power_save, 'power_save', True)
        b(self.dark, 'dark', False)
        s(self.rename_folder, 'rename_folder')
        s(self.rename_from, 'rename_from')
        s(self.rename_to, 'rename_to')
        b(self.rename_recursive, 'rename_recursive', False)
        b(self.rename_autodetect, 'rename_autodetect', True)
        s(self.detect_folder, 'detect_folder')
        b(self.detect_recursive, 'detect_recursive', True)
        b(self.detect_only_bad, 'detect_only_bad', True)
        s(self.crack_out, 'crack_out')
        b(self.crack_extract, 'crack_extract', True)
        b(self.crack_autorename, 'crack_autorename', True)
        b(self.crack_nested, 'crack_nested', True)
        b(self.crack_record, 'crack_record', False)
        b(self.crack_swap, 'crack_swap', True)

    def _save_config(self):
        """把当前勾选项与文件夹路径写入 config.json，下次启动自动恢复。"""
        cfg = {
            'power_save': self.power_save.get(),
            'dark': self.dark.get(),
            'rename_folder': self.rename_folder.get(),
            'rename_from': self.rename_from.get(),
            'rename_to': self.rename_to.get(),
            'rename_recursive': self.rename_recursive.get(),
            'rename_autodetect': self.rename_autodetect.get(),
            'detect_folder': self.detect_folder.get(),
            'detect_recursive': self.detect_recursive.get(),
            'detect_only_bad': self.detect_only_bad.get(),
            'crack_out': self.crack_out.get(),
            'crack_extract': self.crack_extract.get(),
            'crack_autorename': self.crack_autorename.get(),
            'crack_nested': self.crack_nested.get(),
            'crack_record': self.crack_record.get(),
            'crack_swap': self.crack_swap.get(),
        }
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 主题 ----------
    def _apply_theme(self, dark):
        if HAVE_SVTTK:
            sv_ttk.set_theme('dark' if dark else 'light')
        else:
            self._apply_custom_theme(dark)
        self.theme_btn.config(text='☀️ 亮色' if dark else '🌙 暗色')

    def _apply_custom_theme(self, dark):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass
        bg = '#1e1e1e' if dark else '#f3f3f3'
        fg = '#ffffff' if dark else '#222222'
        accent = '#0a84ff'
        style.configure('.', background=bg, foreground=fg)
        style.configure('TLabel', background=bg, foreground=fg)
        style.configure('TFrame', background=bg)
        style.configure('TNotebook', background=bg)
        style.configure('TNotebook.Tab', background=bg, foreground=fg, padding=[10, 6])
        style.configure('TButton', padding=[10, 5])
        style.configure('Accent.TButton', background=accent, foreground='white')
        style.map('Accent.TButton', background=[('active', '#3a9bff')])

    def toggle_theme(self):
        self.dark.set(not self.dark.get())
        self._apply_theme(self.dark.get())

    # ---------- 低功耗模式 ----------
    def _maybe_apply_power(self):
        """在重任务开始前调用：若开启低功耗模式且尚未应用，则限制 CPU 核数 + 降优先级。"""
        if self.power_save.get() and not self._power_applied:
            n = powersave.apply_power_saving(0.5)
            self._power_applied = True
            return n
        return None

    def _on_power_toggle(self):
        # 勾选时下一次任务会自动应用；取消勾选则尽量恢复为全核（仅提示，不改已限制状态）
        if self.power_save.get():
            self._append_text(self.crack_log, powersave.describe(0.5))
        else:
            self._append_text(self.crack_log,
                              '已关闭低功耗模式：任务将以全速运行（注意机器温度）')

    # ---------- 通用工具 ----------
    def _mk_log(self, widget):
        def log(msg):
            self.root.after(0, self._append_text, widget, msg)
        return log

    def _append_text(self, widget, msg):
        widget.insert('end', str(msg) + '\n')
        widget.see('end')

    def _finish_busy(self, btn):
        self._busy = False
        btn.config(state='normal')

    # ---------- 进度条（提取/扫描时的可视化反馈） ----------
    def _init_progress(self, tab, frame, row):
        """在 frame 的 row / row+1 处放置进度条与状态文字。"""
        pb = ttk.Progressbar(frame, orient='horizontal', mode='determinate')
        pb.grid(row=row, column=0, columnspan=3, sticky='ew', padx=8, pady=(2, 0))
        label = ttk.Label(frame, text='')
        label.grid(row=row + 1, column=0, columnspan=3, sticky='w', padx=8, pady=(0, 4))
        self.progress_widgets[tab] = (pb, label)

    def _set_progress(self, tab, kw):
        pb, label = self.progress_widgets[tab]
        mode = kw.get('mode')
        if mode == 'indeterminate':
            if pb.cget('mode') != 'indeterminate':
                pb.config(mode='indeterminate')
            pb.start(20)
        elif mode == 'determinate':
            if pb.cget('mode') == 'indeterminate':
                pb.stop()
            pb.config(mode='determinate')
        if 'value' in kw:
            pb.config(value=kw['value'])
        if 'max' in kw:
            pb.config(maximum=kw['max'])
        if 'text' in kw:
            label.config(text=kw['text'])

    def _progress(self, tab, **kw):
        # 由工作线程调用，统一切回主线程更新 UI（after 仅支持位置参数，故打包成 dict）
        self.root.after(0, self._set_progress, tab, kw)

    def _make_progress_cb(self, tab):
        """生成传给 archivelib 的 progress 回调（phase: start/step/extract/done）。"""
        def cb(current=0, total=1, name='', phase='step'):
            if phase == 'start':
                self._progress(tab, mode='determinate', value=0, max=total or 1,
                               text='准备处理 %d 个压缩包…' % (total or 0))
            elif phase == 'step':
                val = max(0, current - 1)
                self._progress(tab, mode='determinate', value=val, max=total or 1,
                               text='处理中 %d/%d：%s' % (current, total or 1, name))
            elif phase == 'extract':
                self._progress(tab, mode='indeterminate',
                               text='正在解压：%s' % (name or ''))
            elif phase == 'done':
                if total and total > 0:
                    self._progress(tab, mode='determinate', value=total, max=total,
                                   text='完成：共处理 %d 个压缩包' % total)
                else:
                    self._progress(tab, mode='determinate', value=0, max=1,
                                   text='完成：没有需要处理的压缩包')
        return cb

    def pick_dir(self, var):
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    def _build_opts(self, out_dir, extract, log, nested=True, record=False, swap=True, auto_rename=True, progress_cb=None):
        return {
            'out_dir': out_dir,
            'extract': extract,
            'recursive_nested': nested,
            'record_password': record,
            'auto_swap': swap,
            'auto_rename': auto_rename,
            'log': log,
            'progress': progress_cb,
            'need_password': self.prompter.request,
            'record_cb': self._record_password,
            'max_depth': 12,
            'seen': set(),
            # 低功耗模式：每处理完一个文件/嵌套层后让出 CPU，避免持续打满
            'throttle': 0.05 if self.power_save.get() else 0,
        }

    # ======================================================================
    # 标签一：批量改后缀
    # ======================================================================
    def build_rename_tab(self):
        f = ttk.Frame(self.notebook)
        self.notebook.add(f, text='批量改后缀')

        ttk.Label(f, text='文件夹:').grid(row=0, column=0, sticky='w', padx=8, pady=4)
        self.rename_folder = tk.StringVar()
        ttk.Entry(f, textvariable=self.rename_folder).grid(row=0, column=1, padx=4, sticky='ew')
        ttk.Button(f, text='选择文件夹', command=lambda: self.pick_dir(self.rename_folder)).grid(row=0, column=2, padx=4)

        self.rename_recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text='包含子文件夹', variable=self.rename_recursive).grid(row=1, column=1, sticky='w', padx=4)

        ttk.Label(f, text='原后缀(如 rar，留空或 * 表示全部文件):').grid(row=2, column=0, sticky='w', padx=8, pady=4)
        self.rename_from = tk.StringVar()
        ttk.Entry(f, textvariable=self.rename_from).grid(row=2, column=1, sticky='ew', padx=4)

        ttk.Label(f, text='新后缀(如 7z；勾选“自动检测”时按真实格式，此项忽略)').grid(row=3, column=0, sticky='w', padx=8, pady=4)
        self.rename_to = tk.StringVar()
        ttk.Entry(f, textvariable=self.rename_to).grid(row=3, column=1, sticky='ew', padx=4)

        self.rename_autodetect = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='自动检测实际格式并改名到正确后缀(开启时忽略原/新后缀)', variable=self.rename_autodetect).grid(row=4, column=1, sticky='w', padx=4)

        self.rename_btn = ttk.Button(f, text='开始改后缀', style='Accent.TButton', command=self.run_rename)
        self.rename_btn.grid(row=5, column=1, sticky='w', pady=8)

        self.rename_log = scrolledtext.ScrolledText(f, height=18)
        self.rename_log.grid(row=6, column=0, columnspan=3, sticky='nsew', padx=8, pady=8)

        self._init_progress('rename', f, 7)

        f.columnconfigure(1, weight=1)
        f.rowconfigure(10, weight=1)

    def run_rename(self):
        if self._busy:
            return
        folder = self.rename_folder.get()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror('错误', '请先选择有效文件夹')
            return
        frm = self.rename_from.get().strip().lstrip('.').lower()
        to = self.rename_to.get().strip().lstrip('.').lower()
        autodetect = self.rename_autodetect.get()
        if not autodetect and not to:
            messagebox.showerror('错误', '请填写新后缀（或勾选“自动检测”按真实格式处理）')
            return

        self._busy = True
        self.rename_btn.config(state='disabled')
        log = self._mk_log(self.rename_log)
        recursive = self.rename_recursive.get()
        self._save_config()  # 记住本次选择

        def worker():
            self._maybe_apply_power()
            count = 0
            to_process = set()
            try:
                # 内容感知改名：按文件真实格式改名到正确后缀（“目标已存在”时跳过，
                # 不影响其它文件）；同时收集识别出的压缩包用于下方“真实格式汇报”。
                # 注意：本标签不做任何密码验证/解压——“尝试密码本”是独立功能。
                try:
                    count, renamed, to_process = archivelib.plan_and_rename(
                        folder, recursive, autodetect, frm, to,
                        log=log, power_save=self.power_save.get())
                except Exception as e:
                    log('[错误] 改名阶段异常: %s' % e)
                log('--- 共修改 %d 个文件 ---' % count)
                if count == 0 and not to_process:
                    log('（无文件需要改名）')

                # 仅汇报识别到的压缩包真实格式（detect_format 只看文件头，极快；
                # 不碰密码、不解压，保证改名流程本身始终很快）
                if to_process:
                    try:
                        shown = archivelib.dedup_archives(to_process)
                    except Exception:
                        shown = [(p, os.path.basename(p)) for p in to_process]
                    log('--- 识别到的压缩包（%d 个，多卷已去重）---' % len(shown))
                    for p, name in shown:
                        try:
                            fmt, _ = archivelib.detect_format(p)
                        except Exception as e:
                            log('⚠ %s | 识别失败: %s' % (name, e))
                            continue
                        log('%s | 实际%s' % (name, fmt or '非压缩格式'))
            except Exception as e:
                # 任何未预见异常都记录，绝不让 worker 静默死亡
                try:
                    log('[严重错误] 处理流程异常: %s' % e)
                except Exception:
                    pass
            finally:
                self.root.after(0, self._finish_busy, self.rename_btn)

        threading.Thread(target=worker, daemon=True).start()

    # ======================================================================
    # 标签二：压缩包检测
    # ======================================================================
    def build_detect_tab(self):
        f = ttk.Frame(self.notebook)
        self.notebook.add(f, text='压缩包检测')

        ttk.Label(f, text='文件夹:').grid(row=0, column=0, sticky='w', padx=8, pady=4)
        self.detect_folder = tk.StringVar()
        ttk.Entry(f, textvariable=self.detect_folder).grid(row=0, column=1, padx=4, sticky='ew')
        ttk.Button(f, text='选择文件夹', command=lambda: self.pick_dir(self.detect_folder)).grid(row=0, column=2, padx=4)

        self.detect_recursive = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='包含子文件夹', variable=self.detect_recursive).grid(row=1, column=1, sticky='w', padx=4)

        self.detect_only_bad = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='仅显示异常(格式不符/损坏)', variable=self.detect_only_bad).grid(row=1, column=2, sticky='w', padx=4)

        ttk.Button(f, text='将异常项加入破解队列', command=self.send_to_crack).grid(row=1, column=0, sticky='w', padx=4, pady=6)
        ttk.Button(f, text='一键改为正确后缀', command=self.rename_detected).grid(row=2, column=0, sticky='w', pady=6)
        self.detect_btn = ttk.Button(f, text='开始检测', command=self.run_detect)
        self.detect_btn.grid(row=2, column=1, sticky='w', pady=6)
        self.trypw_btn = ttk.Button(f, text='尝试密码本(快速验证)', command=self.run_try_passwords)
        self.trypw_btn.grid(row=2, column=2, sticky='w', pady=6)

        cols = (('name', '文件名', 220), ('ext', '后缀', 70), ('fmt', '实际格式', 90),
                ('status', '状态', 180), ('note', '说明', 320))
        self.detect_tree = ttk.Treeview(f, columns=[c[0] for c in cols], show='headings', height=16)
        for key, title, width in cols:
            self.detect_tree.heading(key, text=title)
            self.detect_tree.column(key, width=width, stretch=(key == 'note'))
        self.detect_tree.grid(row=3, column=0, columnspan=3, sticky='nsew', padx=8, pady=8)

        self.detect_log = scrolledtext.ScrolledText(f, height=8)
        self.detect_log.grid(row=4, column=0, columnspan=3, sticky='nsew', padx=8, pady=4)
        self._init_progress('detect', f, 5)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(3, weight=2)
        f.rowconfigure(4, weight=1)

        self.detect_results = []

    def run_detect(self):
        if self._busy:
            return
        folder = self.detect_folder.get()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror('错误', '请选择文件夹')
            return
        self._busy = True
        self.detect_btn.config(state='disabled')
        log = self._mk_log(self.detect_log)
        self._save_config()
        recursive = self.detect_recursive.get()
        self._save_config()

        def worker():
            self._maybe_apply_power()
            try:
                def prog(i, total, p):
                    self.root.after(0, log, '检测中 %d/%d: %s' % (i, total, os.path.basename(p)))
                    self._progress('detect', mode='determinate', value=i, max=total,
                                   text='检测中 %d/%d：%s' % (i, total, os.path.basename(p)))
                res = archivelib.scan_folder(folder, recursive, prog)
                self.detect_results = res
                self.root.after(0, self._fill_detect_tree)
                self.root.after(0, log, '检测完成，共扫描 %d 个文件' % len(res))
                n = len(res)
                self._progress('detect', mode='determinate', value=n, max=n or 1,
                               text='检测完成，共 %d 个文件' % n)
            except Exception as e:
                self.root.after(0, log, '[错误] 检测流程异常: %s' % e)
            finally:
                self.root.after(0, self._finish_busy, self.detect_btn)

        threading.Thread(target=worker, daemon=True).start()

    def _collect_archives(self, folder, recursive):
        """轻量收集文件夹里的压缩包路径（仅看文件头 magic，不解压、不整包校验，
        对 4GB+ 大文件也瞬间完成）。返回绝对路径列表。"""
        out = []
        if recursive:
            walker = os.walk(folder)
        else:
            files = [n for n in os.listdir(folder)
                     if os.path.isfile(os.path.join(folder, n))]
            walker = [(folder, [], files)]
        for root, _d, files in walker:
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    fmt, _ = archivelib.detect_format(p)
                except Exception:
                    fmt = None
                if fmt is not None:
                    out.append(p)
        return out

    def run_try_passwords(self):
        """“尝试密码本”独立功能：仅用密码本把每个加密压缩包试一遍密码，命中即弹窗提示。
        与改后缀完全分离；不破解、不解压；多卷去重；速度 = 密码本长度 × 压缩包数。
        """
        if self._busy:
            return
        folder = self.detect_folder.get()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror('错误', '请先在“压缩包检测”标签选择文件夹')
            return
        passwords = self.get_passwords()
        if not passwords:
            messagebox.showerror('错误', '密码本为空，请先在“密码本 & 破解解压”标签填写密码')
            return

        self._busy = True
        self.trypw_btn.config(state='disabled')
        log = self._mk_log(self.detect_log)
        recursive = self.detect_recursive.get()
        self._save_config()

        def worker():
            self._maybe_apply_power()
            try:
                archives = self._collect_archives(folder, recursive)
                try:
                    deduped = archivelib.dedup_archives(archives)
                except Exception as e:
                    log('⚠ 多卷去重失败: %s（将逐文件尝试）' % e)
                    deduped = [(p, os.path.basename(p)) for p in archives]
                total = len(deduped)
                self._progress('detect', mode='determinate', value=0, max=total or 1,
                               text='尝试密码本：共 %d 个压缩包' % total)
                log('--- 尝试密码本（仅验证，不破解/不解压，共 %d 个，多卷已去重）---' % total)
                if total == 0:
                    log('（未识别到任何压缩包）')
                found = []
                for idx, (test_path, name) in enumerate(deduped, 1):
                    try:
                        fmt, _ = archivelib.detect_format(test_path)
                    except Exception as e:
                        log('⚠ %s | 识别失败: %s' % (name, e))
                        continue
                    if fmt is None:
                        log('⚠ %s | 非压缩格式，跳过' % name)
                        continue
                    # try_passwords 仅对“加密”压缩包遍历一次密码本；未加密会立即返回
                    try:
                        pwd = archivelib.try_passwords(test_path, fmt, passwords)
                    except Exception as e:
                        log('  ⚠ %s | 验证异常: %s' % (name, e))
                        pwd = None
                    if pwd is None:
                        log('  %s | 实际%s | 需要密码，密码本未命中' % (name, fmt))
                    elif pwd == '':
                        log('  %s | 实际%s | 未加密，可直接解压' % (name, fmt))
                    else:
                        log('  %s | 实际%s | ✓ 密码本命中: %s' % (name, fmt, pwd))
                        found.append((name, pwd))
                    self._progress('detect', mode='determinate', value=idx, max=total or 1,
                                   text='验证中 %d/%d：%s' % (idx, total, name))
                if found:
                    msg = '密码本中找到以下 %d 个文件的正确密码：\n\n' % len(found)
                    msg += '\n'.join('• %s  →  %s' % (n, pw) for n, pw in found)
                    msg += '\n\n如需解压，请到“密码本 & 破解解压”标签勾选“找到密码后自动解压”。'
                    self.root.after(0, lambda m=msg: messagebox.showinfo('密码本命中提示', m))
                else:
                    log('（密码本中未找到任何压缩包的正确密码）')
                self._progress('detect', mode='determinate', value=total, max=total or 1,
                               text='完成：共尝试 %d 个压缩包' % total)
            except Exception as e:
                log('[错误] 尝试密码本异常: %s' % e)
            finally:
                self.root.after(0, self._finish_busy, self.trypw_btn)

        threading.Thread(target=worker, daemon=True).start()

    def _fill_detect_tree(self):
        self.detect_tree.delete(*self.detect_tree.get_children())
        for info in self.detect_results:
            is_archive_claim = info['ext'] in archivelib.ARCHIVE_EXTS
            candidate = (info['fmt'] is not None) or is_archive_claim
            if not candidate:
                continue
            bad = info['status'] in BAD_STATUSES
            if self.detect_only_bad.get() and not bad:
                continue
            self.detect_tree.insert('', 'end', values=(
                info['name'], info['ext'], info['fmt'] or '未知',
                info['status'], info['note']))

    def send_to_crack(self):
        if not self.detect_results:
            messagebox.showinfo('提示', '请先执行检测')
            return
        added = 0
        existing = set(self.crack_list.get(0, 'end'))
        for info in self.detect_results:
            is_archive_claim = info['ext'] in archivelib.ARCHIVE_EXTS
            if (info['fmt'] is not None) or is_archive_claim:
                if info['path'] not in existing:
                    self.crack_list.insert('end', info['path'])
                    existing.add(info['path'])
                    added += 1
        self.notebook.select(2)
        self._append_text(self.crack_log, '已加入 %d 个压缩包到破解队列' % added)

    def rename_detected(self):
        """把检测出的“后缀不符”的压缩包自动改名为正确后缀，并刷新列表。"""
        if self._busy:
            return
        if not self.detect_results:
            messagebox.showinfo('提示', '请先执行检测')
            return
        bad = [i for i in self.detect_results
               if i['fmt'] is not None and i['ext'] != archivelib.FORMAT_EXT.get(i['fmt'])]
        if not bad:
            messagebox.showinfo('提示', '没有需要改名的文件（后缀均已正确）')
            return
        log = self._mk_log(self.detect_log)
        self._busy = True
        self.detect_btn.config(state='disabled')

        def worker():
            self._maybe_apply_power()
            changed = 0
            try:
                for info in bad:
                    np = archivelib.auto_rename_archive(info['path'], log)
                    if np != info['path']:
                        info['path'] = np
                        info['ext'] = os.path.splitext(np)[1].lower() or '(无)'
                        changed += 1
                    # 改名后重新分析，确认状态（此时多半为“一致”或“加密”）
                    ni = archivelib.analyze_file(np)
                    info['status'] = ni['status']
                    info['note'] = ni['note']
                    info['fmt'] = ni['fmt']
                    info['fmt_ext'] = ni['fmt_ext']
                    info['encrypted'] = ni['encrypted']
                self.root.after(0, self._fill_detect_tree)
                log('--- 已自动改名 %d 个文件 ---' % changed)
            except Exception as e:
                log('[错误] 改名流程异常: %s' % e)
            finally:
                self.root.after(0, self._finish_busy, self.detect_btn)

        threading.Thread(target=worker, daemon=True).start()

    # ======================================================================
    # 标签三：密码本 & 破解解压
    # ======================================================================
    def build_crack_tab(self):
        f = ttk.Frame(self.notebook)
        self.notebook.add(f, text='密码本 & 破解解压')

        ttk.Label(f, text='密码本(每行一个密码):').grid(row=0, column=0, sticky='w', padx=8, pady=4)
        self.pw_text = scrolledtext.ScrolledText(f, height=9)
        self.pw_text.grid(row=1, column=0, rowspan=5, sticky='nsew', padx=8, pady=4)
        ttk.Button(f, text='保存密码本', command=self.save_passwords).grid(row=1, column=1, sticky='w', padx=4)
        ttk.Button(f, text='载入常用密码', command=self.load_default_passwords).grid(row=2, column=1, sticky='w', padx=4)

        ttk.Label(f, text='待处理压缩包:').grid(row=0, column=2, sticky='w', padx=8, pady=4)
        self.crack_list = tk.Listbox(f, height=12, selectmode='extended')
        self.crack_list.grid(row=1, column=2, rowspan=4, sticky='nsew', padx=8, pady=4)
        ttk.Button(f, text='添加文件(可多选)', command=self.add_crack_files).grid(row=5, column=2, sticky='w', padx=8)
        ttk.Button(f, text='从文件夹扫描', command=self.add_crack_folder).grid(row=6, column=2, sticky='w', padx=8)
        ttk.Button(f, text='清空列表', command=lambda: self.crack_list.delete(0, 'end')).grid(row=7, column=2, sticky='w', padx=8)

        ttk.Label(f, text='解压输出文件夹:').grid(row=8, column=0, sticky='w', padx=8, pady=4)
        self.crack_out = tk.StringVar()
        ttk.Entry(f, textvariable=self.crack_out).grid(row=8, column=1, sticky='ew', padx=4)
        ttk.Button(f, text='选择', command=lambda: self.pick_dir(self.crack_out)).grid(row=8, column=2, padx=4)

        self.crack_extract = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='找到密码后自动解压', variable=self.crack_extract).grid(row=9, column=1, sticky='w')

        self.crack_autorename = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='自动改为正确后缀(改名后再解压)', variable=self.crack_autorename).grid(row=9, column=2, sticky='w', padx=4)

        self.crack_nested = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='递归解压嵌套压缩包', variable=self.crack_nested).grid(row=10, column=0, sticky='w', padx=8)

        self.crack_record = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text='每成功一次将密码记录到密码本', variable=self.crack_record).grid(row=10, column=1, sticky='w')

        self.crack_swap = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='损坏时自动尝试其它压缩格式', variable=self.crack_swap).grid(row=10, column=2, sticky='w', padx=4)

        ttk.Button(f, text='检测嵌套(选中项)', command=self.run_nested_scan).grid(row=11, column=2, sticky='w', pady=4)
        self.crack_btn = ttk.Button(f, text='开始破解并解压', style='Accent.TButton', command=self.run_crack)
        self.crack_btn.grid(row=11, column=1, sticky='w', pady=4)

        self.crack_log = scrolledtext.ScrolledText(f, height=12)
        self.crack_log.grid(row=12, column=0, columnspan=3, sticky='nsew', padx=8, pady=8)
        self._init_progress('crack', f, 13)
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(2, weight=2)
        f.rowconfigure(12, weight=1)

    # ---------- 密码本 ----------
    def load_passwords(self):
        if os.path.exists(PASSWORD_FILE):
            try:
                with open(PASSWORD_FILE, 'r', encoding='utf-8') as fh:
                    self.pw_text.insert('1.0', fh.read())
            except OSError:
                pass

    def save_passwords(self, silent=False):
        txt = self.pw_text.get('1.0', 'end')
        try:
            with open(PASSWORD_FILE, 'w', encoding='utf-8') as fh:
                fh.write(txt)
            if not silent:
                messagebox.showinfo('已保存', '密码本已保存到 passwords.txt')
        except OSError as e:
            messagebox.showerror('保存失败', str(e))

    def load_default_passwords(self):
        existing = set(self.pw_text.get('1.0', 'end').splitlines())
        added = 0
        for p in DEFAULT_PASSWORDS:
            if p not in existing:
                self.pw_text.insert('end', p + '\n')
                existing.add(p)
                added += 1
        self.save_passwords(silent=True)  # 密码本持久化：变更即保存
        self._append_text(self.crack_log, '已载入 %d 个常用密码' % added)

    def get_passwords(self):
        return [l.strip() for l in self.pw_text.get('1.0', 'end').splitlines() if l.strip()]

    def _record_password(self, pwd):
        if not pwd:
            return
        if pwd in self.get_passwords():
            return
        self.pw_text.insert('end', pwd + '\n')
        self.save_passwords(silent=True)
        self._append_text(self.crack_log, '[记录] 密码已加入密码本: %s' % pwd)

    def add_crack_files(self):
        paths = filedialog.askopenfilenames(title='选择压缩包(可多选)')
        existing = set(self.crack_list.get(0, 'end'))
        for p in paths:
            if p not in existing:
                self.crack_list.insert('end', p)
                existing.add(p)

    def add_crack_folder(self):
        folder = filedialog.askdirectory(title='选择文件夹(将扫描其中所有压缩包)')
        if not folder:
            return
        res = archivelib.scan_folder(folder, True, None)
        existing = set(self.crack_list.get(0, 'end'))
        added = 0
        for info in res:
            if info['fmt'] is not None and info['path'] not in existing:
                self.crack_list.insert('end', info['path'])
                existing.add(info['path'])
                added += 1
        self._append_text(self.crack_log, '从文件夹添加 %d 个压缩包' % added)

    # ---------- 嵌套检测 ----------
    def run_nested_scan(self):
        if self._busy:
            return
        sel = self.crack_list.curselection()
        paths = list(self.crack_list.get(0, 'end'))
        if not paths:
            messagebox.showerror('错误', '请先添加待处理的压缩包')
            return
        path = paths[sel[0]] if sel else paths[0]
        fmt, _ = archivelib.detect_format(path)
        if fmt is None:
            messagebox.showinfo('提示', '该文件不是可识别的压缩包:\n' + os.path.basename(path))
            return
        passwords = self.get_passwords()

        self._busy = True
        log = self._mk_log(self.crack_log)

        def worker():
            self._maybe_apply_power()
            pwd = archivelib.try_passwords(path, fmt, passwords)
            if pwd is None:
                ans = self.prompter.request('压缩包 "%s" 需要密码:' % os.path.basename(path), path=path, fmt=fmt)
                if ans and archivelib.verify_password(path, fmt, ans):
                    pwd = ans
                else:
                    pwd = None
            res = archivelib.scan_nested(path, fmt, pwd or '')
            self.root.after(0, self._show_nested, path, res)
            self.root.after(0, self._finish_busy, self.crack_btn)
            self._busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _show_nested(self, path, res):
        if res is None:
            messagebox.showinfo('嵌套检测',
                                '无法读取压缩包（可能密码错误或已损坏）:\n' + os.path.basename(path))
            return
        nested = [r for r in res if r[1]]
        win = tk.Toplevel(self.root)
        win.title('嵌套检测 · ' + os.path.basename(path))
        win.geometry('600x440')
        txt = scrolledtext.ScrolledText(win)
        txt.pack(fill='both', expand=True, padx=10, pady=10)
        txt.insert('end', '共 %d 个条目，其中 %d 个疑似嵌套压缩包：\n' % (len(res), len(nested)))
        txt.insert('end', '=' * 54 + '\n')
        for name, is_arch, ext, size, enc in res:
            tag = '【嵌套压缩包】' if is_arch else '             '
            enc_s = ' (加密)' if enc else ''
            txt.insert('end', '%s %s%s\n' % (tag, name, enc_s))
        txt.configure(state='disabled')

    # ---------- 破解并解压 ----------
    def run_crack(self):
        if self._busy:
            return
        paths = list(self.crack_list.get(0, 'end'))
        if not paths:
            messagebox.showerror('错误', '请先添加待处理的压缩包')
            return
        passwords = self.get_passwords()
        if not passwords and not messagebox.askyesno('提示', '密码本为空，将继续尝试无密码解压。是否继续?'):
            return
        do_extract = self.crack_extract.get()
        out = self.crack_out.get()
        if do_extract and (not out or not os.path.isdir(out)):
            messagebox.showerror('错误', '请选择有效的解压输出文件夹')
            return

        self._busy = True
        self.crack_btn.config(state='disabled')
        log = self._mk_log(self.crack_log)
        self._save_config()

        def worker():
            self._maybe_apply_power()
            try:
                opts = self._build_opts(out, do_extract, log,
                                        nested=self.crack_nested.get(),
                                        record=self.crack_record.get(),
                                        swap=self.crack_swap.get(),
                                        auto_rename=self.crack_autorename.get(),
                                        progress_cb=self._make_progress_cb('crack'))
                summary = archivelib.process_files(paths, passwords, opts)
                log('=== 汇总 ===')
                log('找到密码/未加密: %d  未找到: %d  解压成功: %d  嵌套: %d  失败/跳过: %d'
                    % (summary['found'], summary['notfound'], summary['extracted'],
                       summary['nested'], summary['failed']))
            except Exception as e:
                log('[错误] 破解/解压流程异常: %s' % e)
            finally:
                self.root.after(0, self._finish_busy, self.crack_btn)

        threading.Thread(target=worker, daemon=True).start()


def main():
    # Windows 高分屏清晰渲染：声明进程为 DPI 感知（需在创建 Tk 之前）。
    # 仅 Windows 需要；Linux/macOS 没有 ctypes.windll，必须跳过，否则启动即崩。
    if sys.platform.startswith('win'):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            pass

    root = tk.Tk()
    app = App(root)
    app._apply_theme(app.dark.get())  # 应用保存的主题（亮/暗）

    # 按屏幕尺寸自适应初始窗口大小，兼顾各种分辨率（含 4K）
    try:
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        w = max(900, int(sw * 0.6))
        h = max(640, int(sh * 0.72))
        root.geometry('%dx%d' % (w, h))
    except Exception:
        root.geometry('1100x760')
    root.minsize(820, 560)

    # 关闭时保存：设置（记住上次选择）+ 密码本（长久保存）
    def _on_close():
        try:
            app._save_config()
            app.save_passwords(silent=True)
        except Exception:
            pass
        root.destroy()
    root.protocol('WM_DELETE_WINDOW', _on_close)
    root.mainloop()


if __name__ == '__main__':
    main()
