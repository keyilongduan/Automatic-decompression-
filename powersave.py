"""进程级省电 / 限核工具。

压缩、解压、加密破解都是高 CPU 操作，在笔记本或散热受限的机器上容易把 CPU
跑到 100% 触发过热 / 电源保护而掉电死机。这个模块通过两种方式给本进程「降频」：

  1. 限制 CPU 亲和性（affinity）：只允许进程使用一部分核，从源头压住总功耗；
  2. 降低进程优先级（BELOW_NORMAL）：让系统其它任务优先，避免被抢占式打满。

仅 Windows 下用 ctypes 实现；其它平台尽量用 sched_setaffinity / nice。任何一步
失败都静默跳过，不影响主流程。

用法：
    import powersave
    n = powersave.apply_power_saving(cpu_fraction=0.5)   # 最多用一半核
"""
import os
import sys

# Windows 常量
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_PROCESS_ALL_ACCESS = 0x1F0FFF


def apply_power_saving(cpu_fraction=0.5, low_priority=True):
    """限制本进程最多使用 cpu_fraction 比例的 CPU 核，并设为低优先级。

    返回实际生效的核数（失败 / 不支持返回 None）。幂等，可重复调用。
    """
    try:
        count = os.cpu_count() or 1
    except Exception:
        count = 1
    if count <= 1:
        # 只有 1 核的机器，限核无意义，仅尝试降优先级
        used = 1
    else:
        used = max(1, int(round(count * cpu_fraction)))
        used = min(used, count)
    mask = (1 << used) - 1

    applied = False
    if sys.platform.startswith('win'):
        try:
            import ctypes
            k = ctypes.windll.kernel32
            # 用基础类型声明，避免依赖 wintypes 中不一定存在的 DWORD_PTR：
            #   HANDLE    -> c_void_p（指针宽）
            #   亲和性掩码 -> c_size_t（指针宽，等价于 DWORD_PTR）
            k.GetCurrentProcess.restype = ctypes.c_void_p
            k.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            k.SetProcessAffinityMask.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
            h = k.GetCurrentProcess()
            if low_priority:
                k.SetPriorityClass(h, _BELOW_NORMAL_PRIORITY_CLASS)
            k.SetProcessAffinityMask(h, ctypes.c_size_t(mask))
            applied = True
        except Exception:
            applied = False
    else:
        try:
            os.sched_setaffinity(0, set(range(used)))
            applied = True
        except Exception:
            applied = False
        if low_priority:
            try:
                os.nice(10)
            except Exception:
                pass

    return used if applied else None


def describe(cpu_fraction=0.5):
    """返回一行人类可读的省电说明（不含副作用）。"""
    try:
        count = os.cpu_count() or 1
    except Exception:
        count = 1
    if count <= 1:
        return '低功耗模式：单核机器，已降低进程优先级'
    used = max(1, min(count, int(round(count * cpu_fraction))))
    return '低功耗模式：限制使用 %d / %d 个 CPU 核，并降低进程优先级' % (used, count)
