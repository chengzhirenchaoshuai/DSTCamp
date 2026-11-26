"""把文件或目录移到 Windows 回收站（可从回收站还原）。"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004            # 不显示进度窗口
_FOF_NOCONFIRMATION = 0x0010    # 不再弹系统的"确定要删除吗"（程序里已经确认过或无需确认）
_FOF_ALLOWUNDO = 0x0040         # 放进回收站而不是直接删除
_FOF_NOERRORUI = 0x0400         # 出错时不弹系统错误框，由程序自己提示
_FOF_WANTNUKEWARNING = 0x4000   # 放不进回收站（太大/该盘没有回收站）时，由系统询问是否永久删除


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def move_to_recycle_bin(path: Path) -> None:
    """把 path 整体移到回收站，失败或用户取消时抛 OSError。

    链接一律拒绝：解析后会变成链接目标，把别处的真实内容移走；调用方应用 os.rmdir/unlink 只删链接。"""
    target = Path(os.path.abspath(path))  # 规范化成完整路径但不跟随链接（resolve 会跟随）
    if target.is_symlink() or os.path.isjunction(target):
        raise OSError(f"拒绝把链接移到回收站：{target}")
    if not target.exists():
        raise FileNotFoundError(f"路径不存在：{target}")
    operation = _SHFILEOPSTRUCTW()
    operation.wFunc = _FO_DELETE
    operation.pFrom = str(target) + "\0"  # pFrom 必须以两个 \0 结尾（ctypes 会再补一个）
    operation.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT
                        | _FOF_NOERRORUI | _FOF_WANTNUKEWARNING)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if operation.fAnyOperationsAborted:
        raise OSError("操作已取消")
    if result != 0:
        raise OSError(f"移到回收站失败（错误码 0x{result:X}），文件可能正被游戏或其它程序占用")
    if target.exists():
        raise OSError("移到回收站后目标仍然存在，可能有文件被占用")
