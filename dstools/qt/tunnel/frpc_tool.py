"""自建节点与 Lolia 共用的原版 frpc：发布版不打包，首次要启动时在后台自动下载。

开服、崩溃自动重启都会拉起 frpc，属于无人值守流程，所以下载不弹窗；失败时由两个面板的状态行
显示“客户端下载失败”，点按钮用 tool_download 的日志窗口重试（失败时提示从网盘工具包导入）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from dstools.i18n import t
from dstools.qt.threads import run_async
from dstools.qt.tool_download import download_tool_with_dialog
from dstools.shared import tool_store
from dstools.shared.resource_paths import runtime_tool_path

FRPC_RELATIVE = "frp_selfhost/frpc.exe"
FRPC_ARCHIVE = FRPC_RELATIVE + ".xz"

_waiters: list[Callable[[], None]] = []
_listeners: list[Callable[[], None]] = []
_downloading = False
_failed_error: str | None = None


def frpc_exe_path() -> Path:
    return runtime_tool_path(FRPC_RELATIVE)


def add_listener(callback: Callable[[], None]) -> None:
    """下载状态变化（开始、成功、失败）时在界面线程回调，面板据此刷新状态行。"""
    _listeners.append(callback)


def downloading() -> bool:
    return _downloading


def failed_error() -> str | None:
    """上次后台下载失败的原因；下载成功或重新开始后清空。"""
    return _failed_error


def ensure(exe: Path, then: Callable[[], None]) -> bool:
    """exe 已存在返回 True；否则后台下载（同时只下一份），下好后在界面线程调用 then()，返回 False。"""
    global _downloading, _failed_error
    if exe.is_file():
        return True
    _waiters.append(then)
    if _downloading:
        return False
    _downloading, _failed_error = True, None
    _notify()
    run_async(lambda: tool_store.download_tool(FRPC_ARCHIVE), lambda _path: _finish(None), _finish)
    return False


def download_with_dialog(parent, then: Callable[[], None]) -> None:
    """用户点“下载客户端”时：带进度的日志窗口，失败时窗口里提示从网盘工具包导入。"""

    def done(_path) -> None:
        global _failed_error
        _failed_error = None
        _notify()
        then()

    download_tool_with_dialog(parent, FRPC_ARCHIVE, t("selfhost.frpc_download_title"), done)


def _finish(exc: Exception | None) -> None:
    global _downloading, _failed_error
    _downloading = False
    _failed_error = None if exc is None else str(exc) or type(exc).__name__
    waiters = _waiters[:] if exc is None else []
    _waiters.clear()
    _notify()
    for callback in waiters:
        callback()


def _notify() -> None:
    for callback in _listeners:
        callback()
