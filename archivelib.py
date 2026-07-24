"""压缩包核心逻辑库：格式识别、损坏检测、密码尝试、解压、嵌套递归。

不依赖任何 GUI 组件，可在无界面环境下单独测试。

RAR / RAR5 处理优先使用内嵌的 UnRAR（tools/UnRAR.exe，可合法随包分发），
它对中文密码、RAR5 加密、多卷的支持最稳；ZIP / 7Z 处理优先使用系统已安装的
完整版 7-Zip（7z.exe，多线程、最快），回退到内嵌的 7za.exe。所有解压/校验
均走原生工具，对大文件远快于 Python 库；仅当原生工具全部缺失时才回退 py7zr/zipfile。

密码校验性能要点：RAR 用 UnRAR `lb`(裸列表，只解密目录头、不解压文件体) 校验，
近乎零成本；ZIP/7Z 用 7-Zip `t`(测试解压) 校验，原生多线程且错误密码会快速失败。
无需 Python 的 rarfile 库，也无需用户自行安装软件（工具已随包分发）。可处理：
  * RAR / RAR5 读取、列出、测试与解压（含中文密码）
  * 多卷压缩包（.part1.rar / .part2.rar ...），只需给出首卷
  * 带密码的压缩包（尝试密码 / 校验密码）

对“被改名成错误后缀的多卷 RAR”（如 audiodude.part1.7z ... part6.7z），本模块会自动
在同目录建立同名硬链接（.partN.rar）让解压器识别为卷集，解压后清理硬链接，
不改动用户原始文件。
"""
import os
import re
import time
import shutil
import zipfile
import tempfile
import subprocess

# 已知压缩格式的文件头签名 (magic bytes)
SIGNATURES = [
    (b'PK\x03\x04', 'ZIP', '.zip'),
    (b'PK\x05\x06', 'ZIP', '.zip'),
    (b'PK\x07\x08', 'ZIP', '.zip'),
    (b'7z\xbc\xaf\x27\x1c', '7Z', '.7z'),
    (b'Rar!\x1a\x07\x00', 'RAR', '.rar'),
    (b'Rar!\x1a\x07\x01\x00', 'RAR5', '.rar'),
    (b'\x1f\x8b', 'GZIP', '.gz'),
    (b'BZh', 'BZIP2', '.bz2'),
    (b'\xfd7zXZ\x00', 'XZ', '.xz'),
    (b'\x28\xb5\x2f\xfd', 'ZSTD', '.zst'),
    (b'\x04\x22M\x18', 'LZ4', '.lz4'),
    (b'MSCF', 'CAB', '.cab'),
]

FORMAT_EXT = {
    'ZIP': '.zip', '7Z': '.7z', 'RAR': '.rar', 'RAR5': '.rar',
    'GZIP': '.gz', 'BZIP2': '.bz2', 'XZ': '.xz', 'ZSTD': '.zst',
    'LZ4': '.lz4', 'CAB': '.cab', 'TAR': '.tar',
}

# 所有压缩格式对应的常见后缀集合
ARCHIVE_EXTS = set(FORMAT_EXT.values())

# 当文件被判定“损坏”时，按顺序尝试用这些格式去打开（换后缀思路）
# 注意：格式名统一使用大写（与 detect_format 返回值一致）
COMMON_ARCHIVE_KINDS = ['ZIP', '7Z', 'RAR']

# 多卷压缩包命名：name.partN.ext
_RAR_PART_RE = re.compile(r'^(?P<base>.+?)\.part(?P<n>\d+)\.(?P<ext>[^.]+)$', re.IGNORECASE)


# --------------------------------------------------------------------------
# 7-Zip（内嵌）相关工具
# --------------------------------------------------------------------------
def sevenzip_bin():
    """定位 7-Zip 可执行文件。优先用系统中已安装的完整版 7z.exe（多线程、格式最全、
    解压最快），回退到项目内 tools/ 自带的 7za.exe / 7z.exe，最后查 PATH。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        r'C:\Program Files\7-Zip\7z.exe',
        r'C:\Program Files (x86)\7-Zip\7z.exe',
        os.path.join(here, 'tools', '7z.exe'),
        os.path.join(here, 'tools', '7za.exe'),
        '7z.exe',
        '7za.exe',
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
        if not os.path.isabs(c):
            p = shutil.which(c)
            if p:
                return p
    return None


def unrar_bin():
    """定位 UnRAR 解压器。优先用项目内 tools/ 自带（已嵌入，可合法随包分发），
    回退系统 WinRAR 安装目录与 PATH。UnRAR 对中文密码/加密 RAR5/多卷支持最稳。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, 'tools', 'UnRAR.exe'),
        os.path.join(here, 'tools', 'Rar.exe'),
        'UnRAR.exe',
        'Rar.exe',
        r'C:\Program Files\WinRAR\UnRAR.exe',
        r'C:\Program Files\WinRAR\Rar.exe',
        r'C:\Program Files (x86)\WinRAR\UnRAR.exe',
        r'C:\Program Files (x86)\WinRAR\Rar.exe',
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
        if not os.path.isabs(c):
            p = shutil.which(c)
            if p:
                return p
    return None


def _unrar_run(args, password=None, timeout=3600):
    """运行 UnRAR，返回 (returncode, stdout, stderr)。

    始终显式提供 stdin=DEVNULL：UnRAR 遇到无法用系统代码页(GBK)表示的密码字符
    （如带圈数字 ⑨, U+2468）时，若用 `-p密码` 传入会解析失败后回退到交互式从 stdin
    读密码；若 stdin 是管道且迟迟不写，就会永久阻塞（表现为“尝试密码本卡死数分钟”）。
    给空 stdin 让 UnRAR 立刻 EOF 失败而非等待输入，彻底根治卡死。正常带 `-p` 的调用
    根本不会读 stdin，DEVNULL 无任何副作用；无密码的 `lb` 调用也不会再卡在等待输入。
    """
    binp = unrar_bin()
    if not binp:
        raise RuntimeError('未找到 UnRAR（请把 UnRAR.exe 放到 tools/ 目录，或安装 WinRAR）')
    cmd = [binp] + list(args)
    if password:
        cmd.append('-p' + password)
    cmd.append('-y')  # 对所有询问回答 yes
    # 关键：stdin 必须指向空设备，绝不能继承/等待管道输入
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, timeout=timeout)
    out = proc.stdout.decode('utf-8', 'ignore')
    err = proc.stderr.decode('utf-8', 'ignore')
    return proc.returncode, out, err


def _unrar_is_bad(rc, out, err):
    """UnRAR 输出是否表示“密码错误 / 打开失败 / 损坏”。0=成功，其它均视为失败。"""
    low = (out + err).lower()
    if 'incorrect password' in low:
        return True
    if 'wrong password' in low:
        return True
    # UnRAR 返回码：0 成功；1 警告（仍解压成功，视为可用）；其它为失败
    if rc not in (0, 1):
        return True
    return False


def _unrar_dest(dest):
    """UnRAR 解压目标目录需以路径分隔符结尾，否则会把内容解压成 dest 前缀文件。"""
    if dest.endswith(os.sep) or dest.endswith('/'):
        return dest
    return dest + os.sep


def _7z_run(args, password=None, timeout=3600, discard=False):
    """运行 7-Zip，返回 (returncode, stdout, stderr)。

    discard=True 时把 stdout 丢弃（重定向到空设备），用于“仅验证密码、不解压全档”
    的场景——把单个小条目抽到 /dev/null，避免把整个大压缩包读进内存。
    """
    binp = sevenzip_bin()
    if not binp:
        raise RuntimeError('未找到 7-Zip（请把 7za.exe 放到 tools/ 目录，或安装 7-Zip）')
    cmd = [binp] + list(args)
    if password:
        cmd.append('-p' + password)
    cmd.append('-y')  # 对所有询问回答 yes
    proc = subprocess.run(cmd,
                          stdout=(subprocess.DEVNULL if discard else subprocess.PIPE),
                          stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL,
                          timeout=timeout)
    out = proc.stdout.decode('utf-8', 'ignore') if not discard else ''
    err = proc.stderr.decode('utf-8', 'ignore')
    return proc.returncode, out, err


def _7z_is_bad(rc, out, err):
    """7-Zip 输出是否表示“打不开 / 密码错误 / 损坏”。"""
    low = (out + err).lower()
    if 'wrong password' in low:
        return True
    if 'cannot open' in low and 'as archive' in low:
        return True
    if 'data error' in low:
        return True
    if rc == 2:
        return True
    return False


def _7z_err(out, err):
    msg = (err or out).strip().splitlines()
    return ' '.join(msg[-3:]) if msg else '7-Zip 执行失败'


def _7z_list(path, password=None):
    """用 7-Zip 列表命令（-slt 技术格式）快速读取归档结构，用于判断“是否加密”/
    校验密码是否正确。仅读目录头、不解压文件体，对超大归档极快。"""
    return _7z_run(['l', '-slt', path], password=password)


def _7z_wrong_pw(rc, out, err):
    """7-Zip 输出是否表示“密码错误”。7z 对错误密码通常返回 rc=2 并打印 Wrong password。

    注意：7z 的 `l`(列目录) 即便密码错误也常返回 rc=0，无法靠返回码区分，因此密码校验
    统一用 `t`(测试解压)；`t` 对错误密码会快速失败（rc=2）而不会解完整个大文件。
    """
    low = (out + err).lower()
    if 'wrong password' in low:
        return True
    if rc == 2:
        return True
    return False


def _rar_volume(path):
    """返回 (首卷路径, 临时硬链接列表) 供 7-Zip 使用。

    若文件是改名后的多卷 RAR（如 .partN.7z），在同目录建立 .partN.rar 硬链接
    （不复制数据），让 7-Zip 识别为卷集；返回首卷 .part1.rar 及需清理的链接列表。
    若后缀已正确（.rar）或并非多卷，则直接返回原路径、空列表。
    """
    m = _RAR_PART_RE.match(os.path.basename(path))
    if not m:
        return path, []
    base, n, ext = m.group('base'), int(m.group('n')), m.group('ext').lower()
    d = os.path.dirname(path)
    if ext == 'rar':
        if n == 1:
            return path, []
        first = os.path.join(d, '%s.part1.rar' % base)
        return (first if os.path.exists(first) else path), []
    # 改名后的多卷：建立 .partN.rar 硬链接
    links = []
    first_vol = None
    k = 1
    while True:
        src = os.path.join(d, '%s.part%d.%s' % (base, k, ext))
        if not os.path.exists(src):
            break
        dst = os.path.join(d, '%s.part%d.rar' % (base, k))
        if not os.path.exists(dst):
            try:
                os.link(src, dst)
                links.append(dst)
            except OSError:
                return path, []  # 无法建立硬链接，回退为单文件处理
        if k == 1:
            first_vol = dst
        k += 1
    if first_vol is not None:
        return first_vol, links
    return path, []


def _rar_cleanup(links):
    for l in links:
        try:
            if os.path.exists(l):
                os.remove(l)
        except OSError:
            pass


def _rar_is_first_volume(path):
    """该文件是否为多卷 RAR 的首卷（避免重复解压同一卷集）。"""
    m = _RAR_PART_RE.match(os.path.basename(path))
    if not m:
        return True
    n = int(m.group('n'))
    if n == 1:
        return True
    prev = os.path.join(os.path.dirname(path),
                        '%s.part%d.%s' % (m.group('base'), n - 1, m.group('ext')))
    return not os.path.exists(prev)


# --------------------------------------------------------------------------
# 基础识别
# --------------------------------------------------------------------------
def detect_format(path):
    """读取文件头，返回 (格式名, 建议后缀)。无法识别返回 (None, None)。"""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return None, None
    for sig, name, ext in SIGNATURES:
        if head.startswith(sig):
            return name, ext
    # TAR 的标识在偏移 257 处的 "ustar"
    try:
        with open(path, 'rb') as f:
            f.seek(257)
            t = f.read(5)
        if t.startswith(b'ustar'):
            return 'TAR', '.tar'
    except OSError:
        pass
    return None, None


def detect_magic_bytes(head):
    """给定文件头字节，返回格式名（或 None）。供内存中判断使用。"""
    for sig, name, _ext in SIGNATURES:
        if head.startswith(sig):
            return name
    if len(head) >= 262 and head[257:262] == b'ustar':
        return 'TAR'
    return None


def is_archive_file(path):
    """判断一个已落盘文件是否为压缩包：后缀命中，或文件头命中 magic。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in ARCHIVE_EXTS:
        return True
    fmt, _ = detect_format(path)
    return fmt is not None


def auto_rename_archive(path, log=None):
    """把被改错后缀的压缩包自动改名为正确后缀；多卷压缩包整体改名。

    返回改名后的（首卷）路径；若无需改名 / 无法改名则原样返回。
    说明：直接对磁盘文件改名（比硬链接可靠，适用于 exFAT/FAT32 等
    不支持硬链接的文件系统）。多卷 RAR 会连同所有同序列卷一起改名。
    """
    if not os.path.exists(path):
        return path
    fmt, _ = detect_format(path)
    if not fmt:
        return path
    correct_ext = FORMAT_EXT.get(fmt)
    if not correct_ext:
        return path
    cur_ext = os.path.splitext(path)[1].lower()
    if cur_ext == correct_ext:
        return path  # 后缀已正确

    d = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    m = _RAR_PART_RE.match(name)
    if m and fmt in ('RAR', 'RAR5'):
        base, n, ext = m.group('base'), int(m.group('n')), m.group('ext').lower()
        first_renamed = None
        k = 1
        while True:
            src = os.path.join(d, '%s.part%d.%s' % (base, k, ext))
            if not os.path.exists(src):
                break
            dst = os.path.join(d, '%s.part%d%s' % (base, k, correct_ext))
            if src != dst and os.path.exists(dst):
                if k == 1:
                    first_renamed = dst  # 目标已存在，跳过但视为已就绪
                k += 1
                continue
            if src != dst:
                try:
                    os.rename(src, dst)
                    if log:
                        log('[改名] %s -> %s' % (os.path.basename(src), os.path.basename(dst)))
                except OSError as e:
                    if log:
                        log('[改名失败] %s: %s' % (os.path.basename(src), e))
                    if k == 1:
                        first_renamed = src
                    k += 1
                    continue
            if k == 1:
                first_renamed = dst if src != dst else src
            k += 1
        return first_renamed if first_renamed else path

    # 单文件（非多卷）
    stem = os.path.splitext(path)[0]
    dst = stem + correct_ext
    if os.path.exists(dst):
        return path
    try:
        os.rename(path, dst)
        if log:
            log('[改名] %s -> %s' % (name, os.path.basename(dst)))
        return dst
    except OSError as e:
        if log:
            log('[改名失败] %s: %s' % (name, e))
        return path


def plan_and_rename(folder, recursive, autodetect, frm, to, log=None, power_save=False):
    """“批量改后缀 + 收集待解压列表”的核心逻辑（与 GUI 解耦，便于单独测试）。

    返回 (count, renamed, to_process)：
      * count     —— 实际改名的文件数
      * renamed   —— 被改名后的文件路径列表
      * to_process—— 待解压的压缩包路径集合（绝对路径，已去重）

    两种模式：
      * autodetect=True（推荐，也是“自动解密解压机器”的默认行为）：
        以文件头 magic 判断真实格式，把误改后缀的压缩包改名到【正确】后缀；
        并收集文件夹里【所有】识别出的压缩包（含后缀已正确、以及“正确后缀已存在”
        的副本），交给解压流水线。这样即便改名因目标已存在而跳过，解压也不会漏掉。
        此时忽略用户手填的 from / to。
      * autodetect=False（纯手动批量改名）：
        盲改 from -> to（原有行为），仅收集刚改名的文件。
    """
    count = 0
    renamed = []
    to_process = set()

    if recursive:
        walker = [(r, fs) for r, _d, fs in os.walk(folder)]
    else:
        walker = [(folder, [n for n in os.listdir(folder)
                            if os.path.isfile(os.path.join(folder, n))])]

    if autodetect:
        if log:
            log('[自动模式] 按文件真实格式处理，忽略“原后缀/新后缀”筛选')

    for root, files in walker:
        for fn in files:
            src = os.path.join(root, fn)
            if autodetect:
                # —— 内容感知：以文件头真实格式为准，而非手动 from/to ——
                fmt, fmt_ext = detect_format(src)
                if fmt is None:
                    continue  # 非压缩包，自动模式下不动它
                # 多卷 RAR：仅首卷参与后续检测/验证，避免对每个分卷重复扫描
                # （一个 4GB×6 的卷集，逐卷扫描会慢数十倍且毫无必要）
                if fmt in ('RAR', 'RAR5') and not _rar_is_first_volume(src):
                    continue
                correct = (fmt_ext or '').lower().lstrip('.')
                cur = os.path.splitext(fn)[1].lower().lstrip('.')
                if cur == correct:
                    to_process.add(os.path.abspath(src))
                    if log:
                        log('[✓] 后缀已正确: %s (%s)' % (fn, fmt))
                    continue
                dst = os.path.join(root, os.path.splitext(fn)[0] + fmt_ext)
                if os.path.exists(dst):
                    # 正确后缀的文件已存在（多半是同一内容的副本），直接用现有文件解压，避免重复
                    to_process.add(os.path.abspath(dst))
                    if log:
                        log('[跳过] 正确后缀 %s 已存在，使用现有文件: %s'
                            % (fmt_ext, os.path.basename(dst)))
                    continue
                try:
                    os.rename(src, dst)
                    count += 1
                    renamed.append(dst)
                    to_process.add(os.path.abspath(dst))
                    if log:
                        log('[改] %s -> %s%s' % (fn, os.path.splitext(fn)[0], fmt_ext))
                except OSError as e:
                    # 改名失败也不放弃：直接用原文件解压
                    to_process.add(os.path.abspath(src))
                    if log:
                        log('[改名失败] %s: %s（将直接解压原文件）' % (fn, e))
                if power_save:
                    time.sleep(0.02)
            else:
                # —— 手动模式：盲改 from -> to（保持原有行为）——
                if frm and frm != '*':
                    if os.path.splitext(fn)[1].lower().lstrip('.') != frm:
                        continue
                stem, oldext = os.path.splitext(fn)
                if oldext.lower().lstrip('.') == to:
                    continue
                dst = os.path.join(root, stem + '.' + to)
                if os.path.exists(dst):
                    if log:
                        log('[跳过] 目标已存在: %s' % dst)
                    continue
                try:
                    os.rename(src, dst)
                    count += 1
                    renamed.append(dst)
                    if log:
                        log('[改] %s -> %s.%s' % (fn, stem, to))
                except OSError as e:
                    if log:
                        log('[改名失败] %s: %s' % (fn, e))
                if power_save:
                    time.sleep(0.02)

    if not autodetect:
        to_process = set(os.path.abspath(p) for p in renamed)
    return count, renamed, to_process


# --------------------------------------------------------------------------
# 结构性打开检查（不看密码，仅判断“是不是这种格式”）
# --------------------------------------------------------------------------
def _open_structural(path, kind):
    """仅判断“能不能以这种格式打开”（不关心密码）。优先原生工具，速度快。"""
    try:
        if kind in ('ZIP', '7Z'):
            binp = sevenzip_bin()
            if binp:
                rc, _out, _err = _7z_run(['l', path])
                return rc in (0, 1)
            if kind == 'ZIP':
                z = zipfile.ZipFile(path)
                z.close()
                return True
            import py7zr
            with py7zr.SevenZipFile(path, 'r') as z:
                _ = z.getnames()
            return True
        if kind in ('RAR', 'RAR5'):
            fv, links = _rar_volume(path)
            try:
                if unrar_bin():
                    # 注意：UnRAR lb 对“非 RAR 垃圾文件”也可能返回 rc==0（但输出为空），
                    # 故同时要求有实际列表输出，避免把任意文件误判为 RAR。
                    rc, out, _err = _unrar_run(['lb', fv])
                    return rc == 0 and bool(out.strip())
                if sevenzip_bin():
                    rc, _out, _err = _7z_run(['l', fv])
                    return rc in (0, 1)
                return False
            finally:
                _rar_cleanup(links)
    except Exception:
        return False
    return False


def detect_format_robust(path, log=None):
    """更稳妥的格式识别：先看 magic；若 magic 识别出的格式打不开（疑似损坏/
    误判），则依次尝试用其它常见格式打开，命中即用。

    返回 (格式名, 说明)；都打不开返回 (None, 原因)。
    """
    fmt, _ = detect_format(path)
    if fmt and _open_structural(path, fmt):
        return fmt, 'magic 识别为 %s' % fmt
    for kind in COMMON_ARCHIVE_KINDS:
        if _open_structural(path, kind):
            if log:
                log('  [换格式] 以 %s 方式成功打开（原检测可能误判/损坏）' % kind)
            return kind, '通过换格式识别为 %s' % kind
    return None, '无法以任何已知格式打开'


def needs_password(path, fmt):
    """该压缩包是否需要密码（供“压缩包检测”标签页快速判断）。"""
    try:
        if fmt in ('ZIP', '7Z'):
            binp = sevenzip_bin()
            if binp:
                rc, out, _err = _7z_list(path)
                # 7z 列表：加密归档会带 “Encrypted = +” 且 rc==0；完全打不开(如头加密/损坏) rc!=0
                if 'Encrypted = +' in out:
                    return True
                return rc not in (0, 1)
            # 回退 Python 库
            if fmt == 'ZIP':
                zf = zipfile.ZipFile(path)
                enc = any(i.flag_bits & 0x1 for i in zf.infolist())
                zf.close()
                return enc
            import py7zr
            with py7zr.SevenZipFile(path, 'r') as z:
                return z.needs_password()
        if fmt in ('RAR', 'RAR5'):
            return _rar_needs_pw(path)
    except Exception:
        return False
    return False


def _rar_needs_pw(path):
    """RAR 是否加密：用 UnRAR 裸列表(lb) 判断。头加密的 RAR5 无密码 lb 会失败(rc!=0)，
    未加密则 rc==0。比 t(完整解压校验) 快得多，对超大归档零成本。"""
    fv, links = _rar_volume(path)
    try:
        if unrar_bin():
            rc, _out, _err = _unrar_run(['lb', fv])
            return rc != 0
        if sevenzip_bin():
            rc, out, _err = _7z_run(['l', '-slt', fv])
            if rc not in (0, 1):
                return False
            return 'Encrypted = +' in out
        return False
    finally:
        _rar_cleanup(links)


# --------------------------------------------------------------------------
# 单文件分析（供“压缩包检测”标签页使用）
# --------------------------------------------------------------------------
def _zip_integrity(path, info):
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        info['status'] = '损坏'
        info['note'] = 'ZIP 文件损坏或无法打开'
        return
    try:
        bad = zf.testzip()
        encrypted = any(m.flag_bits & 0x1 for m in zf.infolist())
        if encrypted:
            info['note'] += '；加密(需密码)'
            info['encrypted'] = True
        if bad:
            info['status'] = '损坏'
            info['note'] += '；CRC 校验失败'
    except RuntimeError:
        # 加密但没给密码，testzip 会抛 RuntimeError
        info['note'] += '；加密(需密码)'
        info['encrypted'] = True
    finally:
        zf.close()


def _sevenzip_integrity(path, info):
    binp = sevenzip_bin()
    if not binp:
        try:
            import py7zr
        except ImportError:
            info['note'] += '；(未安装 7-Zip/py7zr，跳过完整性校验)'
            return
        try:
            with py7zr.SevenZipFile(path, 'r') as z:
                if z.needs_password():
                    info['note'] += '；加密(需密码)'
                    info['encrypted'] = True
        except Exception as e:
            info['note'] += '；(校验失败: %s)' % e
        return
    # 原生 7-Zip：列表即可判断是否加密/能否打开（失败也要优雅降级，不能让调用方崩溃）
    try:
        rc, out, _err = _7z_run(['l', '-slt', path])
        if 'Encrypted = +' in out:
            info['note'] += '；加密(需密码)'
            info['encrypted'] = True
        elif rc not in (0, 1):
            info['status'] = '损坏'
            info['note'] += '；无法打开(可能损坏)'
    except Exception as e:
        info['note'] += '；(校验失败: %s)' % e


def _rar_integrity(path, info):
    if not unrar_bin():
        info['note'] += '；（未找到 UnRAR，跳过校验）'
        return
    fv, links = _rar_volume(path)
    try:
        # 用裸列表(lb)判断：能列出(rc==0)说明格式正常；失败多半是头加密或无法打开。
        rc, _out, _err = _unrar_run(['lb', fv])
        if rc == 0:
            return
        # 头加密的 RAR5 无密码 lb 会失败 —— 视为加密(需密码)
        info['note'] += '；加密(需密码)'
        info['encrypted'] = True
    except Exception as e:
        # 工具缺失/某卷损坏等异常必须降级，绝不能上抛杀掉调用线程
        info['note'] += '；(校验失败: %s)' % e
    finally:
        _rar_cleanup(links)


def analyze_file(path):
    """分析单个文件：识别真实格式、判断后缀是否一致、是否损坏/加密。"""
    ext = (os.path.splitext(path)[1] or '').lower()
    fmt, fmt_ext = detect_format(path)
    info = {
        'path': path,
        'name': os.path.basename(path),
        'ext': ext or '(无)',
        'fmt': fmt,
        'fmt_ext': fmt_ext,
        'status': '一致',
        'note': '',
        'encrypted': False,
    }
    if fmt is None:
        if ext in ARCHIVE_EXTS:
            info['status'] = '疑似损坏/非压缩文件'
            info['note'] = '后缀为 %s，但文件头不是已知压缩格式' % ext
        else:
            info['status'] = '非压缩文件'
            info['note'] = '非压缩格式，无需检测'
        return info

    if ext != fmt_ext:
        info['status'] = '格式不符(疑似误改后缀)'
        info['note'] = '实际为 %s 格式，建议后缀改为 %s' % (fmt, fmt_ext)
    else:
        info['note'] = '确认为 %s 格式' % fmt

    # 完整性/加密检查：任何异常都降级为“跳过校验”，绝不能上抛导致调用方（GUI 线程）崩溃
    try:
        if fmt == 'ZIP':
            _zip_integrity(path, info)
        elif fmt == '7Z':
            _sevenzip_integrity(path, info)
        elif fmt in ('RAR', 'RAR5'):
            _rar_integrity(path, info)
    except Exception as e:
        info['note'] += '；(完整性检查出错: %s)' % e

    # 损坏/疑似损坏时，尝试换格式识别（覆盖“7z 改名 rar 后误判为损坏”等场景）
    try:
        if info['status'] in ('损坏', '疑似损坏/非压缩文件'):
            rfmt, rnote = detect_format_robust(path, None)
            if rfmt is not None and rfmt != fmt:
                info['fmt'] = rfmt
                info['fmt_ext'] = FORMAT_EXT.get(rfmt)
                info['status'] = '格式不符(疑似误改后缀)'
                info['note'] = '换格式后识别为 %s' % rfmt
                info['encrypted'] = needs_password(path, rfmt)
    except Exception as e:
        info['note'] += '；(换格式识别出错: %s)' % e
    return info


def scan_folder(folder, recursive, progress_cb=None):
    """扫描文件夹，返回每个文件的分析结果列表。"""
    files = []
    if recursive:
        for root, _dirs, names in os.walk(folder):
            for n in names:
                files.append(os.path.join(root, n))
    else:
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            if os.path.isfile(p):
                files.append(p)
    total = len(files)
    results = []
    for i, p in enumerate(files, 1):
        results.append(analyze_file(p))
        if progress_cb:
            progress_cb(i, total, p)
    return results


# --------------------------------------------------------------------------
# 密码尝试 / 校验 / 解压
# --------------------------------------------------------------------------
def dedup_archives(paths):
    """多卷压缩包去重：把同一个多卷集的多个分卷折叠成“首卷”，避免对每个分卷都重复
    试密码（对 4GB×N 的多卷集，这是把 6 次试密码降到 1 次的关键提速）。

    返回 [(test_path, display_name), ...]，顺序保持；RAR/RAR5 多卷只保留首卷，
    其它格式原样保留。display_name 用首卷文件名，便于日志/弹窗展示。
    """
    out = []
    seen = set()
    for p in paths:
        try:
            fmt, _ = detect_format(p)
        except Exception:
            fmt = None
        if fmt in ('RAR', 'RAR5'):
            try:
                fv, _ = _rar_volume(p)
            except Exception:
                fv = p
            key = os.path.abspath(fv)
            name = os.path.basename(fv)
        else:
            key = os.path.abspath(p)
            name = os.path.basename(p)
        if key in seen:
            continue
        seen.add(key)
        out.append((p if fmt not in ('RAR', 'RAR5') else key, name))
    return out


def try_passwords(path, fmt, passwords):
    """尝试密码字典。返回 '' 表示无需密码，返回密码字符串表示找到，None 表示未找到。

    性能要点（针对大文件）：
      * RAR/RAR5 用 UnRAR 的 `lb`(裸列表) 校验密码——只解密目录头、不解压文件体，
        对任意大小几乎零成本；正确/错误/无密码分别得到 rc=0/11/12，区分可靠。
        调用方应通过 dedup_archives() 先把多卷折叠成首卷，避免重复试密码。
      * 7Z/ZIP 用 7-Zip 的 `t`(测试解压) 校验密码——原生多线程、比 Python 库快得多；
        错误密码会快速失败(rc=2)而不会解完整个大文件。注意：固态(solid) 7z 无法对
        单个条目单独做 CRC 校验，故对 7Z 必须整包 `t` 才能保证正确（不能“只测最小条目”）。
    """
    # 跳过空/空白密码：空密码传给命令行会变成 `-p`（无参数），导致 UnRAR/7z 进入
    # 交互式等待输入而挂起，表现为“尝试密码本非常慢/卡死”。这是必须防御的脆弱点。
    passwords = [p for p in (passwords or []) if p]

    if fmt in ('ZIP', '7Z'):
        binp = sevenzip_bin()
        if not binp:
            # 回退：Python 库（慢，仅当系统/嵌入 7-Zip 都缺失时）
            return _try_passwords_py(fmt, path, passwords)
        # 先判断是否需要密码（l 列表很快，不解压）
        rc, out, err = _7z_list(path)
        listable = rc in (0, 1)
        enc = listable and ('Encrypted = +' in out)
        if listable and not enc:
            return ''
        # 逐个试密码本（t 测试解压，原生、错误密码快速失败）
        # 注意：对 7Z 不能用“只测单个条目”来提速——固态(solid) 7z 的单个条目无法
        # 单独校验 CRC，会误判错误密码为正确；整包 `t` 是唯一可靠方式。
        for pwd in passwords:
            rc2, out2, err2 = _7z_run(['t', path], password=pwd)
            if not _7z_wrong_pw(rc2, out2, err2):
                return pwd
        # 原生 7z 可能因 ZIP 密码以 UTF-8 存储(如 pyzipper 创建)而判错，退回 pyzipper 精确字节校验
        if fmt == 'ZIP':
            py = _zip_py_pw(path, passwords)
            if py is not None:
                return py
        # 列表失败（如头加密/损坏）但仍想确认是否其实无密码：再用空密码试一次
        if not listable:
            rc3, out3, err3 = _7z_run(['t', path])
            if not _7z_wrong_pw(rc3, out3, err3):
                return ''
        return None

    if fmt in ('RAR', 'RAR5'):
        fv, links = _rar_volume(path)
        try:
            if unrar_bin():
                # 裸列表：rc==0 表示未加密（或无头加密）；否则需密码
                # 单卷 lb 只解密目录头、极快；设 120s 上限，防御任何异常挂起
                rc0, _o0, _e0 = _unrar_run(['lb', fv], timeout=120)
                if rc0 == 0:
                    return ''
                for pwd in passwords:
                    rc, _o, _e = _unrar_run(['lb', fv], password=pwd, timeout=120)
                    if rc == 0:
                        return pwd
                return None
            if sevenzip_bin():
                rc0, out0, _err0 = _7z_run(['l', '-slt', fv])
                listable = rc0 in (0, 1)
                enc = listable and ('Encrypted = +' in out0)
                if listable and not enc:
                    return ''
                for pwd in passwords:
                    rc, out, err = _7z_run(['t', fv], password=pwd)
                    if not _7z_wrong_pw(rc, out, err):
                        return pwd
                if not listable:
                    rc, out, err = _7z_run(['t', fv])
                    if not _7z_wrong_pw(rc, out, err):
                        return ''
                return None
            return None
        finally:
            _rar_cleanup(links)

    return None


def _zip_py_pw(path, passwords):
    """用 pyzipper 试密码（精确字节密码，支持 AES 与 Legacy ZipCrypto）。

    7-Zip 命令行按系统代码页(如 GBK)解读 -p，若 ZIP 的密码以 UTF-8 存储
    （如 pyzipper 创建）会判“错误密码”；pyzipper 用我们传入的精确字节，可正确匹配。
    仅作为原生 7z 失败时的回退（速度较慢）。
    """
    try:
        import pyzipper
    except ImportError:
        return None
    try:
        z = pyzipper.AESZipFile(path)
    except Exception:
        return None
    try:
        try:
            z.testzip()
            return ''
        except RuntimeError:
            pass
        for pwd in passwords:
            try:
                z.setpassword(pwd.encode('utf-8', 'ignore'))
                if z.testzip() is None:
                    return pwd
            except Exception:
                continue
        return None
    finally:
        z.close()


def _zip_py_extract(path, password, dest):
    import pyzipper
    pw = password if password else None
    with pyzipper.AESZipFile(path) as z:
        if pw:
            z.setpassword(pw.encode('utf-8', 'ignore'))
        z.extractall(dest)


def _zip_py_scan(path, password):
    import pyzipper
    pw = password or None
    out = []
    with pyzipper.AESZipFile(path) as z:
        if pw:
            z.setpassword(pw.encode('utf-8', 'ignore'))
        for i in z.infolist():
            ext = os.path.splitext(i.filename)[1].lower()
            out.append((i.filename, ext in ARCHIVE_EXTS, ext,
                        getattr(i, 'file_size', 0), True))
    return out


def _try_passwords_py(fmt, path, passwords):
    """Python 库回退（仅当 7-Zip 不可用时）。速度慢，不推荐用于大文件。"""
    if fmt == 'ZIP':
        return _zip_py_pw(path, passwords)
    if fmt == '7Z':
        try:
            import py7zr
        except ImportError:
            return None
        tmp = tempfile.mkdtemp()
        try:
            try:
                with py7zr.SevenZipFile(path, 'r') as z:
                    z.extractall(tmp)
                return ''
            except Exception:
                pass
            for pwd in passwords:
                try:
                    with py7zr.SevenZipFile(path, 'r', password=pwd) as z:
                        z.extractall(tmp)
                    return pwd
                except Exception:
                    continue
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return None


def verify_password(path, fmt, password):
    """验证某个密码能否打开该压缩包。优先用原生工具（`t` 测试/`lb` 列表），快且可靠。"""
    try:
        if fmt in ('ZIP', '7Z'):
            binp = sevenzip_bin()
            if binp:
                rc, out, err = _7z_run(['t', path], password=password)
                if not _7z_wrong_pw(rc, out, err):
                    return True
                # 原生可能因 ZIP 密码 UTF-8 编码不符而失败；退回 pyzipper 精确字节校验
                if fmt == 'ZIP':
                    try:
                        import pyzipper
                        z = pyzipper.AESZipFile(path)
                        try:
                            z.setpassword(password.encode('utf-8', 'ignore'))
                            return z.testzip() is None
                        finally:
                            z.close()
                    except Exception:
                        return False
                return False
            # 回退 Python 库
            if fmt == 'ZIP':
                zf = zipfile.ZipFile(path)
                zf.setpassword(password.encode('utf-8', 'ignore'))
                return zf.testzip() is None
            import py7zr
            import tempfile as _t
            import shutil as _s
            with py7zr.SevenZipFile(path, 'r', password=password) as z:
                names = z.getnames()
                if not names:
                    return True
                td = _t.mkdtemp()
                try:
                    z.extract(path=td, targets=[names[0]])
                    return True
                finally:
                    _s.rmtree(td, ignore_errors=True)
        if fmt in ('RAR', 'RAR5'):
            fv, links = _rar_volume(path)
            try:
                if unrar_bin():
                    rc, out, err = _unrar_run(['lb', fv], password=password)
                    return rc == 0
                if sevenzip_bin():
                    rc, out, err = _7z_run(['t', fv], password=password)
                    return not _7z_wrong_pw(rc, out, err)
                return False
            finally:
                _rar_cleanup(links)
    except Exception:
        return False
    return False


def extract_archive(path, fmt, password, dest):
    """解压到 dest 目录。password 为空字符串/None 表示无密码。

    全部走原生工具（7-Zip / UnRAR），对大文件远快于 Python 库；仅在原生工具缺失时
    回退到 py7zr / zipfile。
    """
    pw = password if password else None
    if fmt in ('ZIP', '7Z'):
        binp = sevenzip_bin()
        if binp:
            rc, out, err = _7z_run(['x', path, '-o' + dest], password=pw)
            if not _7z_is_bad(rc, out, err):
                return
            # 原生可能因 ZIP 密码 UTF-8 编码不符而失败；退回 pyzipper 精确字节解压
            if fmt == 'ZIP' and 'wrong password' in (out + err).lower():
                _zip_py_extract(path, pw, dest)
                return
            raise RuntimeError(_7z_err(out, err))
        # 回退 Python 库
        if fmt == 'ZIP':
            with zipfile.ZipFile(path) as zf:
                zf.extractall(dest, pwd=pw.encode('utf-8', 'ignore') if pw else None)
        else:
            import py7zr
            with py7zr.SevenZipFile(path, 'r', password=pw) as z:
                z.extractall(dest)
        return
    if fmt in ('RAR', 'RAR5'):
        fv, links = _rar_volume(path)
        try:
            if unrar_bin():
                rc, out, err = _unrar_run(['x', fv, _unrar_dest(dest)], password=pw)
                if _unrar_is_bad(rc, out, err):
                    raise RuntimeError(_7z_err(out, err))
            elif sevenzip_bin():
                rc, out, err = _7z_run(['x', fv, '-o' + dest], password=pw)
                if _7z_is_bad(rc, out, err):
                    raise RuntimeError(_7z_err(out, err))
            else:
                raise RuntimeError('未找到 UnRAR/7-Zip，无法解压 RAR')
        finally:
            _rar_cleanup(links)
        return
    raise ValueError('不支持的格式: %s' % fmt)


def scan_nested(path, fmt, password):
    """检测压缩包内条目，返回 [(name, is_archive, ext, size, encrypted), ...]。

    无法读取（密码错误/损坏）返回 None。
    """
    pw = password or ''
    out = []
    try:
        if fmt in ('ZIP', '7Z'):
            binp = sevenzip_bin()
            if binp:
                rc, sout, _err = _7z_run(['l', '-slt', path], password=pw)
                if rc not in (0, 1):
                    # 原生可能因 ZIP 密码 UTF-8 编码不符而失败；退回 pyzipper 精确字节读取
                    if fmt == 'ZIP':
                        try:
                            return _zip_py_scan(path, pw)
                        except Exception:
                            return None
                    return None
                cur = {}
                entries = []
                for line in sout.splitlines():
                    if line.startswith('Path = '):
                        if cur:
                            entries.append(cur)
                            cur = {}
                        cur = {'name': line[7:].strip()}
                    elif line.startswith('Size = '):
                        try:
                            cur['size'] = int(line[7:].strip() or 0)
                        except ValueError:
                            cur['size'] = 0
                    elif line.startswith('Encrypted = +'):
                        cur['enc'] = True
                if cur:
                    entries.append(cur)
                for e in entries:
                    name = e.get('name', '')
                    if not name or name.endswith('/'):
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    out.append((name, ext in ARCHIVE_EXTS, ext,
                                e.get('size', 0), e.get('enc', False)))
            elif fmt == 'ZIP':
                with zipfile.ZipFile(path) as zf:
                    for i in zf.infolist():
                        ext = os.path.splitext(i.filename)[1].lower()
                        out.append((i.filename, ext in ARCHIVE_EXTS, ext,
                                    i.file_size, bool(i.flag_bits & 0x1)))
            else:
                import py7zr
                with py7zr.SevenZipFile(path, 'r', password=pw) as z:
                    for n in z.getnames():
                        ext = os.path.splitext(n)[1].lower()
                        out.append((n, ext in ARCHIVE_EXTS, ext, 0, False))
        elif fmt in ('RAR', 'RAR5'):
            fv, links = _rar_volume(path)
            try:
                if unrar_bin():
                    rc, sout, _err = _unrar_run(['lb', fv], password=pw)
                    if rc not in (0, 1):
                        return None
                    # `UnRAR lb` 逐行输出压缩包内文件名；过滤掉自身的版本/提示行
                    noise = ('unrar', '免费软件', '列出', '压缩文件', '属性',
                             '----', '大小', '日期', '时间', '名称')
                    for line in sout.splitlines():
                        name = line.strip()
                        if not name:
                            continue
                        low = name.lower()
                        if any(n in low for n in noise):
                            continue
                        ext = os.path.splitext(name)[1].lower()
                        out.append((name, ext in ARCHIVE_EXTS, ext, 0, False))
                elif sevenzip_bin():
                    rc, sout, _err = _7z_run(['l', '-slt', fv], password=pw)
                    if rc not in (0, 1):
                        return None
                    cur = {}
                    entries = []
                    for line in sout.splitlines():
                        if line.startswith('Path = '):
                            if cur:
                                entries.append(cur)
                                cur = {}
                            cur = {'name': line[7:].strip()}
                        elif line.startswith('Size = '):
                            try:
                                cur['size'] = int(line[7:].strip() or 0)
                            except ValueError:
                                cur['size'] = 0
                        elif line.startswith('Encrypted = +'):
                            cur['enc'] = True
                    if cur:
                        entries.append(cur)
                    for e in entries:
                        name = e.get('name', '')
                        if not name or name.endswith('/'):
                            continue
                        ext = os.path.splitext(name)[1].lower()
                        out.append((name, ext in ARCHIVE_EXTS, ext,
                                    e.get('size', 0), e.get('enc', False)))
                else:
                    return None
            finally:
                _rar_cleanup(links)
    except Exception:
        return None
    return out


# --------------------------------------------------------------------------
# 递归解压主流程（带回调）
# --------------------------------------------------------------------------
def _zero(key=None):
    d = {'found': 0, 'notfound': 0, 'extracted': 0, 'failed': 0, 'nested': 0}
    if key:
        d[key] = 1
    return d


def _unique_dest(out_dir, stem):
    base = os.path.join(out_dir, stem)
    if not os.path.exists(base):
        return base
    i = 2
    while os.path.exists('%s_%d' % (base, i)):
        i += 1
    return '%s_%d' % (base, i)


def _iter_files(folder):
    res = []
    for root, _d, files in os.walk(folder):
        for n in files:
            res.append(os.path.join(root, n))
    return res


def _recursive_extract(path, fmt, passwords, out_dir, opts, depth):
    log = opts['log']
    name = os.path.basename(path)
    if depth > opts.get('max_depth', 12):
        log('  ↳ 达到最大递归深度，停止: %s' % name)
        return _zero()
    seen = opts.setdefault('seen', set())
    ap = os.path.abspath(path)
    if ap in seen:
        return _zero()
    seen.add(ap)

    # 1) 取得密码：先试密码本，再尝试手动输入
    pwd = try_passwords(path, fmt, passwords)
    if pwd is None:
        requester = opts.get('need_password')
        manual = None
        if requester:
            # 限制重试次数，避免 verify_password 异常时死循环把 CPU 跑满
            for _ in range(opts.get('max_pw_attempts', 5)):
                ans = requester('压缩包 "%s" 需要密码，请输入（取消则跳过）：' % name, path=path, fmt=fmt)
                if not ans:
                    break
                if verify_password(path, fmt, ans):
                    manual = ans
                    break
                log('  ↳ 密码不正确，请重试或取消')
        if manual is None:
            log('  ✗ 未能获得密码，跳过: %s' % name)
            return _zero('notfound')
        pwd = manual
        log('  ✓ 手动输入密码成功: %s' % name)
        if opts.get('record_password') and opts.get('record_cb'):
            opts['record_cb'](pwd)
    elif pwd == '':
        log('  ✓ 未加密（无需密码）: %s' % name)
    else:
        log('  ✓ 密码本命中: %s' % pwd)
        if opts.get('record_password') and opts.get('record_cb'):
            opts['record_cb'](pwd)

    summary = _zero()
    summary['found'] = 1
    if not opts.get('extract'):
        return summary

    # 2) 解压（开始解压时上报，进度条进入动态“正在解压”状态）
    prog = opts.get('progress')
    if prog:
        prog(name=name, phase='extract')
    dest = _unique_dest(out_dir, os.path.splitext(name)[0])
    try:
        extract_archive(path, fmt, pwd, dest)
        log('  ⤓ 解压到: %s' % dest)
        summary['extracted'] = 1
    except Exception as e:
        log('  ✗ 解压失败: %s' % e)
        summary['failed'] = 1
        return summary

    # 3) 递归处理嵌套压缩包
    if opts.get('recursive_nested'):
        for item in _iter_files(dest):
            if is_archive_file(item):
                ifmt, inode = detect_format_robust(item, log)
                if ifmt is None:
                    continue
                log('  ↳ 发现嵌套压缩包: %s (%s)' % (os.path.basename(item), ifmt))
                summary['nested'] += 1
                sub = _recursive_extract(item, ifmt, passwords, dest, opts, depth + 1)
                for k in summary:
                    summary[k] += sub[k]
                # 低功耗：解压完一层嵌套后让出 CPU，避免持续满载
                t = opts.get('throttle', 0)
                if t:
                    time.sleep(t)
    return summary


def process_one(path, passwords, opts):
    """处理单个压缩包（自动改正确后缀 + 损坏自动换格式 + 密码本/手动 + 递归嵌套）。

    opts 需包含：out_dir, extract, recursive_nested, record_password,
    auto_swap, auto_rename, log, 以及可选的 need_password / record_cb / max_depth / seen。
    返回汇总字典。
    """
    log = opts['log']
    out_dir = opts['out_dir']
    name = os.path.basename(path)
    if not os.path.exists(path):
        log('[跳过] 文件不存在（可能已被改名）: %s' % name)
        return _zero()
    # 自动改为正确后缀（识别为压缩包后先改名再解压；兼容 exFAT 等不支持硬链接的文件系统）
    if opts.get('auto_rename', True):
        newp = auto_rename_archive(path, log)
        if newp != path:
            path = newp
            name = os.path.basename(path)
    # 优先信任文件头 magic（改名后即为真实格式）；magic 失败再尝试换格式兜底。
    # 注意：多卷 RAR 用“列目录”方式经常误判为打不开（假阴性），故不再依赖它来“识别”，
    # 而是直接相信文件头并尝试解压。
    fmt, _ = detect_format(path)
    note = ('magic 识别为 %s' % fmt) if fmt else ''
    if fmt is None and opts.get('auto_swap', True):
        fmt, note = detect_format_robust(path, log)
    if fmt is None:
        log('[跳过] %s 不能识别为压缩包（%s）' % (name, note or '无法以任何已知格式打开'))
        return _zero('failed')
    # 多卷 RAR：只由首卷统一处理，避免重复解压（process_files 已做去重，这里再兜底）
    if fmt in ('RAR', 'RAR5') and not _rar_is_first_volume(path):
        log('[跳过] %s 属于多卷压缩包的一卷，已由首卷统一处理' % name)
        return _zero()
    log('[处理] %s -> %s（%s）' % (name, fmt, note or 'magic 识别'))
    return _recursive_extract(path, fmt, passwords, out_dir, opts, 0)


def process_files(paths, passwords, opts):
    """批量处理，聚合汇总字典。多卷压缩包自动去重，仅处理首卷。

    若 opts 含 'progress' 回调（签名 prog(current, total, name, phase)），会实时上报
    处理进度：phase 取值 start / step / extract / done。
    """
    log = opts.get('log')
    prog = opts.get('progress')
    deduped = []
    for p in paths:
        m = _RAR_PART_RE.match(os.path.basename(p))
        if m and int(m.group('n')) != 1:
            if log:
                log('[跳过] %s 属于多卷压缩包的一卷，将由首卷统一处理' % os.path.basename(p))
            continue
        deduped.append(p)
    total = len(deduped)
    if total == 0:
        if prog:
            prog(total=0, phase='done')
        return _zero()
    if prog:
        prog(total=total, phase='start')
    summary = _zero()
    done = 0
    for p in deduped:
        done += 1
        if prog:
            prog(current=done, total=total, name=os.path.basename(p), phase='step')
        r = process_one(p, passwords, opts)
        for k in summary:
            summary[k] += r[k]
        # 低功耗：每处理完一个压缩包后让出 CPU
        t = opts.get('throttle', 0)
        if t:
            time.sleep(t)
    if prog:
        prog(total=total, phase='done')
    return summary
